import hashlib
import json
import time
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.ai import knowledge
from app.ai.base import ProviderError
from app.ai.prompts import gap_elicitation_v1 as prompt
from app.ai.schemas import GapAnalysis
from app.core.errors import AppError, feature_disabled
from app.database import new_id
from app.models.entities import Clarification, Draft, Evidence, Task
from app.models.knowledge_ai import KnowledgeAIRun
from app.models.schemas import ClarificationGenerateOut, GeneratedClarificationOut
from app.services import knowledge_service
from app.services.gap_detection import detect_gaps

TYPE_WEIGHTS = {
    "missing_reasoning": 6,
    "missing_cue": 5,
    "changed_condition": 4,
    "missing_exception": 3,
    "beginner_mistake": 2,
    "unobservable_information": 1,
}
FIELDS = ("cue", "interpretation", "decision", "exception")


def _normalized(text):
    return " ".join(text.casefold().split())


def select_candidates(candidates, units, evidence_items, existing_clarifications):
    known = {unit["id"]: unit for unit in units}
    order = {unit["id"]: index for index, unit in enumerate(units)}
    evidence_ids = {item["id"] for item in evidence_items}
    prior = {(row["knowledge_unit_id"], row["gap_type"]) for row in existing_clarifications}
    ranked, warnings = [], []
    for candidate in candidates:
        row = candidate.model_dump() if hasattr(candidate, "model_dump") else dict(candidate)
        unit = known.get(row["knowledge_unit_id"])
        if unit is None:
            warnings.append("CANDIDATE_UNKNOWN_UNIT")
            continue
        citations = row.get("evidence_ids", [])
        unit_ids = set(unit.get("evidence_ids", []))
        if (unit_ids and not citations) or not set(citations) <= (unit_ids & evidence_ids):
            warnings.append("CANDIDATE_INVALID_EVIDENCE")
            continue
        question = row["suggested_question"].strip()
        if not question or not row["requires_expert"]:
            warnings.append("CANDIDATE_NOT_EXPERT_QUESTION")
            continue
        if (unit["id"], row["gap_type"]) in prior:
            continue
        score = (
            row["importance"] * 10
            + TYPE_WEIGHTS[row["gap_type"]]
            + sum(not unit.get(field) for field in FIELDS)
        )
        ranked.append({**row, "question": question, "priority": score})
    ranked.sort(
        key=lambda row: (
            -row["priority"],
            order[row["knowledge_unit_id"]],
            -TYPE_WEIGHTS[row["gap_type"]],
            _normalized(row["question"]),
        )
    )
    selected, texts, pairs = [], set(), set()
    for row in ranked:
        text = _normalized(row["question"])
        pair = (row["knowledge_unit_id"], row["gap_type"])
        if text in texts or pair in pairs:
            continue
        selected.append(row)
        texts.add(text)
        pairs.add(pair)
        if len(selected) == 3:
            break
    return selected, list(dict.fromkeys(warnings))


def _row(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _snapshot(session, task, draft):
    units = deepcopy(draft.knowledge_units)
    ids = {eid for unit in units for eid in unit.get("evidence_ids", [])}
    evidence = [
        _row(row)
        for row in session.scalars(
            select(Evidence)
            .where(Evidence.task_id == task.id, Evidence.id.in_(ids))
            .order_by(Evidence.id)
        )
    ]
    clarifications = [
        _row(row)
        for row in session.scalars(
            select(Clarification)
            .where(Clarification.draft_id == draft.id)
            .order_by(Clarification.created_at, Clarification.id)
        )
    ]
    data = {
        "task": {key: getattr(task, key) for key in ("title", "equipment", "language")},
        "draft_id": draft.id,
        "revision": draft.revision,
        "units": units,
        "evidence_items": evidence,
        "clarifications": clarifications,
    }
    # Hash complete state, not the bounded and text-truncated model context.
    digest = hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()
    return data, digest


def _bounded(data):
    original = data
    data = deepcopy(data)
    data["units"] = data["units"][:30]
    data["evidence_items"] = data["evidence_items"][:60]
    data["clarifications"] = data["clarifications"][-30:]
    for unit in data["units"]:
        for field in ("action", *FIELDS):
            if isinstance(unit.get(field), str):
                unit[field] = unit[field][:1000]
    for row in data["evidence_items"]:
        for field in ("content", "author_label", "source_label"):
            if isinstance(row.get(field), str):
                row[field] = row[field][:1000]
        row.pop("created_at", None)
    for row in data["clarifications"]:
        for field in ("question", "answer"):
            if isinstance(row.get(field), str):
                row[field] = row[field][:1000]
        row.pop("created_at", None)
        row.pop("answered_at", None)
    data["task"] = {key: value[:1000] for key, value in data["task"].items()}

    def prune():
        ids = {eid for unit in data["units"] for eid in unit.get("evidence_ids", [])}
        data["evidence_items"] = [row for row in data["evidence_items"] if row["id"] in ids]

    prune()
    truncated = (
        len(data["units"]) < len(original["units"])
        or len(data["evidence_items"]) < len(original["evidence_items"])
        or len(data["clarifications"]) < len(original["clarifications"])
        or any(
            len(value) > 1000
            for row in [
                *original["units"],
                *original["evidence_items"],
                *original["clarifications"],
                original["task"],
            ]
            for value in row.values()
            if isinstance(value, str)
        )
    )
    while len(json.dumps(data, ensure_ascii=False)) > 64000:
        truncated = True
        if data["units"]:
            data["units"].pop()
            prune()
        elif data["clarifications"]:
            data["clarifications"].pop(0)
        else:
            raise AppError("AI_CONTEXT_TOO_LARGE", "Draft context exceeds the input limit.", 422)
    return data, ["CONTEXT_TRUNCATED"] if truncated else []


def _receipt(session, task_id, req):
    run = session.scalar(
        select(KnowledgeAIRun).where(
            KnowledgeAIRun.task_id == task_id,
            KnowledgeAIRun.operation == "gap_elicitation",
            KnowledgeAIRun.request_id == req.request_id,
        )
    )
    if run is None:
        return None
    if run.output["request"] != req.model_dump(exclude={"request_id"}):
        raise AppError("REQUEST_ID_CONFLICT", "Request ID was used with a different payload.", 409)
    result = deepcopy(run.output["response"])
    current = session.get(Draft, run.draft_id)
    if current is None or current.revision != run.draft_revision:
        result["status"] = "stale"
    return ClarificationGenerateOut.model_validate(result)


def _resolve(session, task_id, draft_id):
    task = session.get(Task, task_id)
    if task is None:
        raise AppError("TASK_NOT_FOUND", "Task not found.", 404)
    draft = session.get(Draft, draft_id)
    if draft is None or draft.task_id != task_id:
        raise AppError("DRAFT_NOT_FOUND", "Draft not found.", 404)
    return task, draft


def _deterministic(data):
    prior = {(row["knowledge_unit_id"], row["gap_type"]) for row in data["clarifications"]}
    units = {unit["id"]: unit for unit in data["units"]}
    evidence_ids = {item["id"] for item in data["evidence_items"]}
    return [
        {
            **candidate.model_dump(),
            "evidence_ids": [
                eid
                for eid in units[candidate.knowledge_unit_id].get("evidence_ids", [])
                if eid in evidence_ids
            ],
            "missing_information": f"Expert clarification needed: {candidate.gap_type}.",
            "rationale": "Deterministic missing-field template; expert review required.",
        }
        for candidate in detect_gaps(data["units"])
        if (candidate.knowledge_unit_id, candidate.gap_type) not in prior
    ]


def generate(factory, settings, task_id, req, user):
    with factory() as session, session.begin():
        if session.get(Task, task_id) is None:
            raise AppError("TASK_NOT_FOUND", "Task not found.", 404)
        replay = _receipt(session, task_id, req)
        if replay is not None:
            return replay
        task, draft = _resolve(session, task_id, req.draft_id)
        knowledge_service.check_revision(session, draft, req.expected_revision)
        if req.mode == "live" and not settings.enable_live_gap_generation:
            raise feature_disabled("ENABLE_LIVE_GAP_GENERATION")
        complete, snapshot_hash = _snapshot(session, task, draft)
    data, warnings = _bounded(complete)
    mode, provider = req.mode, "fixture"
    model_id = prompt_version = latency_ms = usage = error = None
    if req.mode == "live":
        provider, model_id, prompt_version = "gemini", settings.gemini_model, prompt.PROMPT_VERSION
        client = None
        started = time.monotonic()
        try:
            client = knowledge.KnowledgeAI(settings)
            analysis, usage = client.call(prompt.build_prompt(data), GapAnalysis)
            selected, rejected = select_candidates(
                analysis.candidates,
                data["units"],
                data["evidence_items"],
                complete["clarifications"],
            )
            warnings.extend([*analysis.warnings, *rejected])
        except ProviderError as exc:
            if not settings.enable_deterministic_gap_fallback:
                raise AppError(exc.code, exc.message, 503, {"retryable": exc.retryable}) from None
            error = {
                "code": exc.code,
                "message": exc.message,
                "model_id": model_id,
                "prompt_version": prompt_version,
            }
            mode, provider, model_id, prompt_version = "fallback", "fixture", None, None
            warnings.append(f"DETERMINISTIC_FALLBACK:{exc.code}")
            selected = _deterministic({**data, "clarifications": complete["clarifications"]})
        finally:
            latency_ms = int((time.monotonic() - started) * 1000)
            if client is not None:
                client.close()
    else:
        selected = _deterministic({**data, "clarifications": complete["clarifications"]})
    try:
        with factory() as session, session.begin():
            replay = _receipt(session, task_id, req)
            if replay is not None:
                return replay
            task, draft = _resolve(session, task_id, req.draft_id)
            if (
                draft.revision != req.expected_revision
                or _snapshot(session, task, draft)[1] != snapshot_hash
            ):
                raise AppError("GENERATION_STALE", "Draft context changed during generation.", 409)
            questions = []
            for row in selected:
                clarification = Clarification(
                    id=new_id("clar"),
                    task_id=task_id,
                    draft_id=draft.id,
                    knowledge_unit_id=row["knowledge_unit_id"],
                    question=row["question"],
                    gap_type=row["gap_type"],
                    priority=row["priority"],
                    status="pending",
                )
                session.add(clarification)
                questions.append(
                    GeneratedClarificationOut(
                        id=clarification.id,
                        knowledge_unit_id=clarification.knowledge_unit_id,
                        question=clarification.question,
                        gap_type=clarification.gap_type,
                        priority=clarification.priority,
                        status="pending",
                        evidence_ids=row["evidence_ids"],
                        missing_information=row["missing_information"],
                        rationale=row["rationale"],
                    )
                )
            result = ClarificationGenerateOut(
                generation_id=new_id("kair"),
                draft_id=draft.id,
                revision=draft.revision,
                generation_mode=mode,
                status="completed",
                provider=provider,
                model_id=model_id,
                prompt_version=prompt_version,
                warnings=list(dict.fromkeys(warnings)),
                questions=questions,
            )
            session.add(
                KnowledgeAIRun(
                    id=result.generation_id,
                    task_id=task_id,
                    operation="gap_elicitation",
                    request_id=req.request_id,
                    draft_id=draft.id,
                    draft_revision=draft.revision,
                    snapshot_hash=snapshot_hash,
                    generation_mode=mode,
                    provider=provider,
                    model_id=model_id,
                    prompt_version=prompt_version,
                    latency_ms=latency_ms if settings.enable_ai_eval_logging else None,
                    usage=usage if settings.enable_ai_eval_logging else None,
                    output={
                        "request": req.model_dump(exclude={"request_id"}),
                        "response": result.model_dump(mode="json"),
                    },
                    warnings=result.warnings,
                    error=error,
                    requested_by=user.id,
                )
            )
            session.flush()
            return result
    except IntegrityError:
        with factory() as session, session.begin():
            replay = _receipt(session, task_id, req)
            if replay is not None:
                return replay
        raise
