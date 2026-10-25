"""Reproducible illustrative evaluations; default mode never loads credentials."""

import argparse
import hashlib
import json
import sys
import tempfile
import time
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.ai import knowledge  # noqa: E402
from app.ai.fixture_provider import FixtureAIProvider  # noqa: E402
from app.ai.prompts import gap_elicitation_v1  # noqa: E402
from app.ai.prompts.registry import PROMPTS  # noqa: E402
from app.ai.schemas import GapAnalysis, QAProviderResult  # noqa: E402
from app.config import Settings  # noqa: E402
from app.database import build_engine, init_db  # noqa: E402
from app.models.entities import Release, Task  # noqa: E402
from app.models.schemas import AskRequest  # noqa: E402
from app.services import gap_generation_service, qa_service  # noqa: E402
from app.services.qa_matching import rank_units  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def load_dataset(name):
    return json.loads((ROOT / "evals" / f"{name}_v1.json").read_text())


class ScriptedProvider(FixtureAIProvider):
    """Offline adversarial replay still passes through production answer validation."""

    def __init__(self, response):
        self.response = response

    def answer_question(self, question, language, context):
        return QAProviderResult.model_validate(
            {**deepcopy(self.response), "provenance": {"provider": "fixture"}}
        )


def _install_context(factory, context, index):
    task_id, release_id = f"eval_task_{index}", f"eval_release_{index}"
    evidence = [{**row, "task_id": task_id} for row in context["evidence_items"]]
    with factory() as session, session.begin():
        task = Task(
            id=task_id,
            title=context["title"],
            equipment=context["equipment"],
            description="Illustrative synthetic evaluation only",
            language="en",
            source_label=context["source_label"],
            owner_id="expert_001",
            status="published",
        )
        session.add(task)
        session.flush()
        session.add(
            Release(
                id=release_id,
                task_id=task_id,
                version=1,
                source_draft_revision=1,
                content_hash=hashlib.sha256(
                    json.dumps(context, sort_keys=True).encode()
                ).hexdigest(),
                approved_by="reviewer_001",
                approval_note="Illustrative authored evaluation snapshot",
                task_snapshot={"id": task_id, "title": context["title"], "language": "en"},
                knowledge_snapshot=deepcopy(context["knowledge_units"]),
                evidence_snapshot=evidence,
            )
        )
        task.active_release_id = release_id
    return task_id


def _qa_case(factory, settings, case, index, mode):
    task_id = _install_context(factory, case["context"], index)
    provider = (
        ScriptedProvider(case["scripted_response"])
        if "scripted_response" in case
        else FixtureAIProvider()
    )
    # Adversarial replays are always offline, even when measuring other cases live.
    case_settings = (
        settings.model_copy(update={"enable_live_qa": False})
        if "scripted_response" in case
        else settings
    )
    started = time.perf_counter()
    answer = qa_service.ask(
        factory,
        case_settings,
        task_id,
        AskRequest(question=case["question"]),
        SimpleNamespace(id="expert_001"),
        provider,
    )
    elapsed = (time.perf_counter() - started) * 1000
    units = {u["id"]: u for u in case["context"]["knowledge_units"]}
    claims = answer.model_dump()["claims"]
    citations = all(
        c["knowledge_unit_id"] in units
        and bool(c["evidence_ids"])
        and set(c["evidence_ids"]) <= set(case["acceptable_evidence_ids"])
        and set(c["evidence_ids"])
        <= set(units[c["knowledge_unit_id"]]["field_evidence"].get(c["field"], []))
        for c in claims
    )
    support = all(
        c["text"].strip() == units[c["knowledge_unit_id"]].get(c["field"], "").strip()
        for c in claims
    )
    relevance = all(
        c["knowledge_unit_id"] in case["acceptable_unit_ids"]
        and c["field"] in case["acceptable_fields"]
        for c in claims
    )
    selected = rank_units(case["question"], case["context"]["knowledge_units"])
    retrieval = (
        bool({u["id"] for u in selected} & set(case["acceptable_unit_ids"]))
        if case["expected_status"] == "answered"
        else None
    )
    leak = any(marker in answer.model_dump_json() for marker in case["forbidden_markers"])
    checks = dict(
        status_correct=answer.status == case["expected_status"],
        citation_valid=citations,
        exact_field_support=support,
        answer_relevance=relevance,
        no_leak=not leak,
        abstention=(not claims if case["expected_status"] != "answered" else None),
        isolation=(citations and not leak if case["group"] == "isolation" else None),
        retrieval_relevance=retrieval,
    )
    return dict(
        id=case["id"],
        kind="qa",
        group=case["group"],
        status=answer.status,
        expected_status=case["expected_status"],
        checks=checks,
        passed=all(v for k, v in checks.items() if v is not None),
        claims=claims,
        elapsed_ms=round(elapsed, 3),
        model_latency_ms=None,
        execution="offline_adversarial_replay" if "scripted_response" in case else mode,
        human_review_required=True,
    )


def _gap_case(settings, case, mode):
    context = deepcopy(case["context"])
    started = time.perf_counter()
    warnings = []
    if mode == "live":
        client = knowledge.KnowledgeAI(settings)
        try:
            output, _ = client.call(gap_elicitation_v1.build_prompt(context), GapAnalysis)
            candidates = output.candidates
        finally:
            client.close()
        questions, warnings = gap_generation_service.select_candidates(
            candidates, context["units"], context["evidence_items"], context["clarifications"]
        )
    else:
        questions = gap_generation_service._deterministic(context)
    # Independently exercise contextual selection and duplicate boundaries with authored replay.
    replay, replay_warnings = gap_generation_service.select_candidates(
        GapAnalysis.model_validate({"candidates": case["scripted_candidates"]}).candidates,
        context["units"],
        context["evidence_items"],
        context["clarifications"],
    )
    pairs = [(q["knowledge_unit_id"], q["gap_type"]) for q in questions]
    duplicates = len(pairs) - len(set(pairs))
    prior = {tuple(pair) for pair in case["known_duplicate_keys"]}
    unit_evidence = {u["id"]: set(u["evidence_ids"]) for u in context["units"]}
    citations = all(
        bool(q["evidence_ids"])
        and set(q["evidence_ids"]) <= set(case["acceptable_evidence_ids"])
        and set(q["evidence_ids"]) <= unit_evidence.get(q["knowledge_unit_id"], set())
        for q in questions
    )
    relevance = all(
        q["knowledge_unit_id"] in case["eligible_unit_ids"]
        and q["gap_type"] in case["allowed_categories"]
        for q in questions
    )
    coverage = not case["eligible_unit_ids"] or any(
        q["knowledge_unit_id"] in case["eligible_unit_ids"]
        and q["gap_type"] in case["allowed_categories"]
        for q in questions
    )
    limited = mode == "deterministic" and case["detector_coverage_limited"]
    leak = any(m in json.dumps(questions) for m in case["forbidden_markers"])
    checks = dict(
        citation_valid=citations,
        no_duplicates=duplicates == 0 and not (set(pairs) & prior),
        bounded=len(questions) <= 3,
        no_leak=not leak,
        authored_relevance=relevance if not limited else None,
        consequential_gap_coverage=coverage if not limited else None,
        replay_boundary=all(q["knowledge_unit_id"] in case["eligible_unit_ids"] for q in replay),
    )
    return dict(
        id=case["id"],
        kind="gap",
        passed=all(v for v in checks.values() if v is not None),
        checks=checks,
        questions=questions,
        contextual_replay_questions=replay,
        warnings=warnings + replay_warnings,
        duplicate_count=duplicates,
        specificity=sum(
            bool(q.get("question") and q.get("knowledge_unit_id") and q.get("evidence_ids"))
            for q in questions
        ),
        authored_relevance_observed=relevance,
        coverage_observed=coverage,
        limitation="Legacy three-template detector cannot measure this contextual category."
        if limited
        else None,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        model_latency_ms=None,
        human_review_required=True,
    )


def run_evaluations(mode="deterministic"):
    if mode not in {"deterministic", "live"}:
        raise ValueError("mode must be deterministic or live")
    gaps, qa = load_dataset("gaps"), load_dataset("qa")
    cases = []
    with tempfile.TemporaryDirectory(prefix="takumi-evals-") as temp:
        values = dict(
            database_url=f"sqlite:///{temp}/eval.db",
            media_storage_dir=Path(temp) / "media",
            app_env="test",
            demo_signing_secret="offline-evaluation",
            enable_ai_eval_logging=False,
        )
        if mode == "live":
            settings = Settings(
                **values, ai_provider="gemini", enable_live_qa=True, enable_live_gap_generation=True
            )
        else:
            settings = Settings(
                _env_file=None,
                **values,
                ai_provider="fixture",
                gemini_api_key=None,
                enable_live_qa=False,
                enable_live_gap_generation=False,
                enable_live_extraction=False,
            )
        engine = build_engine(settings)
        init_db(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        try:
            for kind, dataset in (("gap", gaps), ("qa", qa)):
                for index, case in enumerate(dataset["cases"]):
                    try:
                        cases.append(
                            _gap_case(settings, case, mode)
                            if kind == "gap"
                            else _qa_case(factory, settings, case, index, mode)
                        )
                    except Exception as exc:
                        cases.append(
                            dict(
                                id=case["id"],
                                kind=kind,
                                passed=False,
                                error=dict(
                                    code=getattr(exc, "code", type(exc).__name__),
                                    message=getattr(exc, "message", "Evaluation runner failed."),
                                ),
                            )
                        )
        finally:
            engine.dispose()
    counts = Counter("error" if "error" in c else "pass" if c["passed"] else "fail" for c in cases)
    metrics = {}
    for key in (
        "retrieval_relevance",
        "citation_valid",
        "exact_field_support",
        "status_correct",
        "abstention",
        "isolation",
        "authored_relevance",
        "consequential_gap_coverage",
    ):
        values = [c["checks"][key] for c in cases if c.get("checks", {}).get(key) is not None]
        metrics[key] = dict(passed=sum(values), measured=len(values))
    supported = [c for c in cases if c.get("group") == "supported"]
    unsupported = [c for c in cases if c.get("group") == "unsupported"]
    return dict(
        run_id=str(uuid4()),
        timestamp=datetime.now(UTC).isoformat(),
        mode=mode,
        dataset_versions=[gaps["dataset_version"], qa["dataset_version"]],
        model_id=settings.gemini_model if mode == "live" else None,
        prompt_versions={s.name: s.version for s in PROMPTS.values()},
        counts={k: counts[k] for k in ("pass", "fail", "error")},
        total=len(cases),
        metrics=metrics,
        supported=dict(passed=sum(c["passed"] for c in supported), total=len(supported), target=9),
        unsupported=dict(
            passed=sum(c["passed"] for c in unsupported), total=len(unsupported), target=5
        ),
        citation_failures=[
            c["id"] for c in cases if c.get("checks", {}).get("citation_valid") is False
        ],
        unsupported_cases=[c["id"] for c in unsupported],
        human_review_cases=[c["id"] for c in cases],
        expert_acceptance=None,
        expert_rejection=None,
        expert_reviewed_semantic_correctness=None,
        gap_duplicate_count=sum(c.get("duplicate_count", 0) for c in cases),
        gap_duplicate_rate=(
            sum(c.get("duplicate_count", 0) for c in cases)
            / sum(len(c.get("questions", [])) for c in cases)
            if any(c.get("questions") for c in cases)
            else None
        ),
        gap_specificity=dict(
            specific_questions=sum(c.get("specificity", 0) for c in cases),
            total_questions=sum(len(c.get("questions", [])) for c in cases if c["kind"] == "gap"),
        ),
        latency=dict(
            measured_total_ms=round(sum(c.get("elapsed_ms", 0) for c in cases), 3),
            model_latency_ms=None,
        ),
        limitations=[
            dict(id=c["id"], reason=c["limitation"]) for c in cases if c.get("limitation")
        ],
        human_review_prerequisite=(
            "Genuine experts must review premises and semantic correctness; "
            "automatic synthetic checks do not establish industrial truth."
        ),
        cases=cases,
    )


def write_report(report, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# Illustrative V3 evaluation",
        "",
        f"Run: {report['run_id']}",
        f"Timestamp: {report['timestamp']}",
        f"Mode: {report['mode']}",
        f"Datasets: {', '.join(report['dataset_versions'])}",
        f"Model: {report['model_id']}",
        f"Prompt versions: {report['prompt_versions']}",
        f"Counts: {report['counts']}",
        f"Supported: {report['supported']}",
        f"Unsupported: {report['unsupported']}",
        f"Latency: {report['latency']}",
        "",
        report["human_review_prerequisite"],
        "",
        "Expert metrics: unmeasured (null).",
        "",
        "## Metrics",
        "",
    ]
    lines += [
        f"- {name}: {value['passed']}/{value['measured']}"
        for name, value in report["metrics"].items()
    ]
    lines += ["", "## Cases and premise review", ""]
    for case in report["cases"]:
        status = "ERROR" if "error" in case else "PASS" if case["passed"] else "FAIL"
        lines.append(f"### {case['id']}: {status}")
        lines.append("```json\n" + json.dumps(case, indent=2) + "\n```")
    output.with_suffix(".md").write_text("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["deterministic", "live"], default="deterministic")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_evaluations(args.mode)
        write_report(report, args.output)
    except Exception as exc:
        print(
            f"Evaluation runner error: {getattr(exc, 'code', type(exc).__name__)}", file=sys.stderr
        )
        return 2
    print(json.dumps({k: report[k] for k in ("counts", "total", "supported", "unsupported")}))
    return 2 if report["counts"]["error"] else 1 if report["counts"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
