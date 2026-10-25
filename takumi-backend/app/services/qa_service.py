import json
import logging
import time
from copy import deepcopy

from app.ai import knowledge
from app.ai.base import ProviderError
from app.ai.prompts import grounded_qa_v1
from app.ai.schemas import ApprovedContext, GroundedAnswer
from app.core.errors import AppError
from app.database import new_id
from app.models.entities import MediaAsset, Task
from app.models.knowledge_ai import KnowledgeAIRun
from app.models.schemas import AskOut
from app.services.evidence_service import interval_problem
from app.services.publication_service import active_release
from app.services.qa_matching import (
    INSUFFICIENT_MSG,
    NEEDS_CONTEXT_MSG,
    question_guard,
    rank_units,
)

logger = logging.getLogger(__name__)


def _live_data(req, context):
    units = rank_units(req.question, context.knowledge_units)
    while True:
        ids = {eid for unit in units for ids in unit.field_evidence.values() for eid in ids}
        data = {
            "question": req.question,
            "language": req.language,
            "release_id": context.release_id,
            "knowledge_units": [unit.model_dump(mode="json") for unit in units],
            "evidence_items": [item for item in context.evidence_items if item["id"] in ids],
        }
        if len(json.dumps(data, ensure_ascii=False)) <= 64000 or not units:
            return data, units
        units.pop()


def _claim_problem(claim, units, evidence, durations, task_id, support_validation):
    unit = units.get(claim.knowledge_unit_id)
    if unit is None:
        return "UNKNOWN_UNIT"
    value = getattr(unit, claim.field, None)
    if value is None:
        return "MISSING_FIELD"
    allowed = set(unit.field_evidence.get(claim.field, [])) & evidence.keys()
    if not claim.evidence_ids or not set(claim.evidence_ids) <= allowed:
        return "INVALID_CITATION"
    for eid in claim.evidence_ids:
        item = evidence[eid]
        if item.get("task_id") != task_id:
            return "WRONG_TASK"
        if item.get("type") == "video_segment" and interval_problem(
            item.get("start_ms"), item.get("end_ms"), durations.get(item.get("media_id"))
        ):
            return "INVALID_INTERVAL"
    if support_validation and claim.text.strip() != value.strip():
        return "UNSUPPORTED_TEXT"
    return None


def ask(factory, settings, task_id, req, user, provider) -> AskOut:
    with factory() as session, session.begin():
        release = active_release(session, task_id)
        release_id, snapshot_hash = release.id, release.content_hash
        context = ApprovedContext(
            release_id=release_id,
            knowledge_units=deepcopy(release.knowledge_snapshot),
            evidence_ids={item["id"] for item in release.evidence_snapshot},
            evidence_items=deepcopy(release.evidence_snapshot),
        )
        durations = {}
        for item in context.evidence_items:
            media_id = item.get("media_id")
            if media_id and media_id not in durations:
                media = session.get(MediaAsset, media_id)
                if media is not None and media.task_id == task_id:
                    durations[media_id] = media.duration_ms
        validation = context.model_copy(deep=True)

    live = settings.enable_live_qa
    guard = question_guard(req.question, req.language, validation.knowledge_units)
    model_id, latency_ms, usage = None, None, None
    called = False
    if guard:
        result = GroundedAnswer(status=guard[0], missing_information=[guard[1]])
        selected = validation.knowledge_units
    elif live:
        data, selected = _live_data(req, validation)
        if not selected:
            result = GroundedAnswer(status="insufficient_evidence")
        else:
            client = knowledge.KnowledgeAI(settings)
            model_id = client.model_id
            started = time.monotonic()
            try:
                called = True
                result, usage = client.call(grounded_qa_v1.build_prompt(data), GroundedAnswer)
            except ProviderError as exc:
                raise AppError(exc.code, exc.message, 503, {"retryable": exc.retryable}) from None
            finally:
                latency_ms = round((time.monotonic() - started) * 1000)
                client.close()
    else:
        selected = validation.knowledge_units
        result = provider.answer_question(req.question, req.language, context)

    units = {unit.id: unit for unit in selected}
    evidence = {item["id"]: item for item in validation.evidence_items}
    claims, dropped = [], []
    for claim in result.claims:
        problem = _claim_problem(
            claim, units, evidence, durations, task_id, settings.enable_qa_support_validation
        )
        if problem:
            dropped.append(problem)
        else:
            claims.append(claim)
    status, missing = result.status, result.missing_information
    review_required = bool(getattr(result, "review_required", False)) if live else False
    if (live and dropped) or (status == "answered" and not claims):
        status, missing, claims = "insufficient_evidence", [INSUFFICIENT_MSG], []
        review_required = live
    if status != "answered":
        claims = []
    if live:
        message = (
            guard[1]
            if guard
            else (NEEDS_CONTEXT_MSG if status == "needs_context" else INSUFFICIENT_MSG)
        )
        missing = [] if status == "answered" else [message]
    answer = AskOut(
        status=status,
        release_id=release_id,
        claims=claims,
        missing_information=missing,
        review_required=review_required,
    )
    with factory() as session, session.begin():
        task = session.get(Task, task_id)
        if task is None or task.active_release_id != release_id:
            raise AppError("RELEASE_CHANGED", "The active release changed during the answer.", 409)
        if called and settings.enable_ai_eval_logging:
            session.add(
                KnowledgeAIRun(
                    id=new_id("kair"),
                    task_id=task_id,
                    operation="grounded_qa",
                    request_id=None,
                    release_id=release_id,
                    snapshot_hash=snapshot_hash,
                    generation_mode="live",
                    provider="gemini",
                    model_id=model_id,
                    prompt_version=grounded_qa_v1.PROMPT_VERSION,
                    latency_ms=latency_ms,
                    usage=usage,
                    output={
                        "status": status,
                        "claim_count": len(claims),
                        "dropped_reason_codes": dropped,
                    },
                    warnings=[],
                    error=None,
                    requested_by=user.id,
                )
            )
            session.flush()
    logger.info("qa task_id=%s release_id=%s status=%s", task_id, release_id, status)
    return answer
