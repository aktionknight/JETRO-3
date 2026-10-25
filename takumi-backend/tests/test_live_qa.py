import json
from copy import deepcopy

import pytest
from sqlalchemy import event, select

from app.ai import knowledge
from app.ai.base import ProviderError, ai_provider
from app.ai.schemas import QAProviderResult
from app.models.entities import Draft, Task
from app.models.knowledge_ai import KnowledgeAIRun
from app.services.qa_matching import INSUFFICIENT_MSG, LANGUAGE_MSG, NEEDS_CONTEXT_MSG
from tests.test_qa import ask


@pytest.fixture
def live_client(make_client):
    with make_client(
        demo_seed_state="approved_baseline",
        ai_provider="gemini",
        gemini_api_key="k",
        gemini_model="gemini-3-test",
        enable_live_qa=True,
    ) as client:
        yield client


def fake_ai(monkeypatch, callback):
    instances = []

    class Fake:
        provider = "gemini"
        model_id = "gemini-3-test"

        def __init__(self, settings):
            self.closed = False
            instances.append(self)

        def call(self, prompt, schema):
            data = json.loads(prompt.split("\nDATA:\n", 1)[1])
            return schema.model_validate(callback(data)), {"total_token_count": 12}

        def close(self):
            self.closed = True

    monkeypatch.setattr(knowledge, "KnowledgeAI", Fake)
    return instances


def good_claim(data, unit_id="ku_001", field="interpretation"):
    unit = next(unit for unit in data["knowledge_units"] if unit["id"] == unit_id)
    return {
        "text": unit[field],
        "knowledge_unit_id": unit_id,
        "field": field,
        "evidence_ids": unit["field_evidence"][field],
    }


@pytest.mark.parametrize(
    "attack",
    [
        "wrong_field",
        "mixed",
        "wrong_task",
        "wrong_release",
        "unit",
        "empty",
        "number",
        "negation",
        "no_claims",
        "partial_invalid",
    ],
)
def test_live_rejects_entire_invalid_answer(live_client, monkeypatch, attack):
    def malicious(data):
        claim = good_claim(data)
        if attack == "wrong_field":
            claim["evidence_ids"] = ["ev_001"]
        elif attack == "mixed":
            claim["evidence_ids"] += ["ev_004"]
        elif attack == "wrong_task":
            claim["evidence_ids"] = ["ev_other_task"]
        elif attack == "wrong_release":
            claim["evidence_ids"] = ["ev_superseded"]
        elif attack == "unit":
            claim["knowledge_unit_id"] = "ku_fake"
        elif attack == "empty":
            claim["evidence_ids"] = []
        elif attack == "number":
            claim["text"] += " Tighten to 99 Nm."
        elif attack == "negation":
            claim = good_claim(data, field="decision")
            claim["text"] = claim["text"].replace("do not", "do")
        claims = [] if attack == "no_claims" else [claim]
        if attack == "partial_invalid":
            claims += [{**claim, "text": "Invented extra instruction"}]
        return {"status": "answered", "claims": claims, "missing_information": ["DANGEROUS advice"]}

    instances = fake_ai(monkeypatch, malicious)
    response = ask(live_client, "Why inspect the bearing housing?")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "insufficient_evidence"
    assert result["claims"] == []
    assert result["review_required"] is True
    assert result["missing_information"] == [INSUFFICIENT_MSG]
    assert "DANGEROUS" not in response.text
    assert instances[0].closed


def test_exact_answer_logs_only_metadata(live_client, monkeypatch):
    seen = []

    def reply(data):
        seen.append(deepcopy(data))
        return {
            "status": "answered",
            "claims": [good_claim(data)],
            "missing_information": ["DANGEROUS advice"],
        }

    fake_ai(monkeypatch, reply)
    response = ask(live_client, "Why inspect the bearing housing?")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "answered"
    assert result["claims"][0] == good_claim(seen[0])
    assert result["missing_information"] == []
    with live_client.app.state.sessionmaker() as session:
        run = session.scalar(select(KnowledgeAIRun))
        assert run.operation == "grounded_qa"
        assert run.release_id == result["release_id"]
        assert run.output == {"status": "answered", "claim_count": 1, "dropped_reason_codes": []}
        assert run.latency_ms >= 0
        assert "housing" not in json.dumps(run.output)
        assert run.request_id is None


@pytest.mark.parametrize(
    "question,status,message",
    [
        ("What torque for housing bolts?", "insufficient_evidence", INSUFFICIENT_MSG),
        ("What tolerance for shaft alignment?", "insufficient_evidence", INSUFFICIENT_MSG),
        ("Is shaft rotation safe at high speed?", "insufficient_evidence", INSUFFICIENT_MSG),
        ("What should the shaft feel like?", "insufficient_evidence", INSUFFICIENT_MSG),
        ("How service a hydraulic excavator?", "insufficient_evidence", INSUFFICIENT_MSG),
        ("Why?", "needs_context", NEEDS_CONTEXT_MSG),
    ],
)
def test_unsupported_never_calls_model(live_client, monkeypatch, question, status, message):
    def forbidden(data):
        pytest.fail("Unsupported question reached provider")

    instances = fake_ai(monkeypatch, forbidden)
    response = ask(live_client, question)
    assert response.status_code == 200
    assert response.json()["status"] == status
    assert response.json()["claims"] == []
    assert response.json()["missing_information"] == [message]
    assert instances == []


def test_language_guard(live_client, monkeypatch):
    fake_ai(monkeypatch, lambda data: pytest.fail("Wrong language reached model"))
    result = ask(live_client, "Why inspect housing?", language="ja").json()
    assert result["status"] == "needs_context"
    assert result["missing_information"] == [LANGUAGE_MSG]


@pytest.mark.parametrize(
    "status,message",
    [
        ("insufficient_evidence", INSUFFICIENT_MSG),
        ("needs_context", NEEDS_CONTEXT_MSG),
    ],
)
def test_model_abstentions_cannot_smuggle_advice(live_client, monkeypatch, status, message):
    fake_ai(
        monkeypatch,
        lambda data: {
            "status": status,
            "claims": [good_claim(data)],
            "missing_information": ["DANGEROUS advice"],
        },
    )
    response = ask(live_client, "Why inspect housing?")
    assert response.json()["claims"] == []
    assert response.json()["missing_information"] == [message]
    assert "DANGEROUS" not in response.text


def test_timeout_closes_client_without_fixture_fallback(live_client, monkeypatch):
    def timeout(data):
        raise ProviderError("PROCESSING_TIMEOUT", "AI request timed out.", True)

    instances = fake_ai(monkeypatch, timeout)
    response = ask(live_client, "Why inspect housing?")
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "PROCESSING_TIMEOUT",
        "message": "AI request timed out.",
        "details": {"retryable": True},
    }
    assert instances[0].closed


def test_call_has_no_transaction_and_rechecks_release(live_client, monkeypatch):
    factory = live_client.app.state.sessionmaker
    engine = factory.kw["bind"]
    checked_out = set()

    def checkout(connection, record, proxy):
        checked_out.add(id(connection))

    def checkin(connection, record):
        checked_out.discard(id(connection))

    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)

    def change_release(data):
        assert not checked_out
        with factory() as session, session.begin():
            session.get(Task, "task_001").active_release_id = None
        return {"status": "answered", "claims": [good_claim(data)]}

    fake_ai(monkeypatch, change_release)
    try:
        response = ask(live_client, "Why inspect housing?")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RELEASE_CHANGED"
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


def test_draft_only_explanation_is_rejected(live_client, monkeypatch):
    with live_client.app.state.sessionmaker() as session, session.begin():
        draft = session.get(Draft, "draft_001")
        units = deepcopy(draft.knowledge_units)
        units[2]["interpretation"] = "DRAFT_ONLY secret explanation"
        draft.knowledge_units = units

    def reply(data):
        assert "DRAFT_ONLY" not in json.dumps(data)
        return {
            "status": "answered",
            "claims": [
                {
                    "text": "DRAFT_ONLY secret explanation",
                    "knowledge_unit_id": "ku_003",
                    "field": "interpretation",
                    "evidence_ids": ["ev_004"],
                }
            ],
        }

    fake_ai(monkeypatch, reply)
    result = ask(live_client, "Why check base mounting fasteners?").json()
    assert result["status"] == "insufficient_evidence"
    assert result["claims"] == []


def test_deterministic_provider_mutation_cannot_expand_snapshot(make_client):
    with make_client(demo_seed_state="approved_baseline") as client:

        class Provider:
            def answer_question(self, question, language, context):
                unit = context.knowledge_units[0]
                unit.interpretation = "MUTATED unsupported advice"
                unit.field_evidence["interpretation"] = ["ev_fake"]
                context.evidence_ids.add("ev_fake")
                context.evidence_items.append({"id": "ev_fake", "task_id": "task_001"})
                return QAProviderResult(
                    status="answered",
                    provenance={"provider": "fake"},
                    claims=[
                        {
                            "text": unit.interpretation,
                            "field": "interpretation",
                            "knowledge_unit_id": unit.id,
                            "evidence_ids": ["ev_fake"],
                        }
                    ],
                )

        client.app.dependency_overrides[ai_provider] = lambda: Provider()
        result = ask(client, "Why inspect housing?").json()
        assert result["status"] == "insufficient_evidence"
        assert result["claims"] == []


def test_logging_can_be_disabled(make_client, monkeypatch):
    fake_ai(monkeypatch, lambda data: {"status": "answered", "claims": [good_claim(data)]})
    with make_client(
        demo_seed_state="approved_baseline",
        ai_provider="gemini",
        gemini_api_key="k",
        gemini_model="gemini-3-test",
        enable_live_qa=True,
        enable_ai_eval_logging=False,
    ) as client:
        assert ask(client, "Why inspect housing?").json()["status"] == "answered"
        with client.app.state.sessionmaker() as session:
            assert session.scalar(select(KnowledgeAIRun)) is None


@pytest.mark.parametrize(
    "start,end,duration,task,reason",
    [
        (-1, 10, None, "task_001", "INVALID_INTERVAL"),
        (10, 10, None, "task_001", "INVALID_INTERVAL"),
        (0, 20, 10, "task_001", "INVALID_INTERVAL"),
        (0, 20, None, "task_other", "WRONG_TASK"),
        (0, 20, None, "task_001", None),
        (0, 20, 20, "task_001", None),
    ],
)
def test_video_claim_boundaries(start, end, duration, task, reason):
    from app.ai.schemas import ApprovedContext
    from app.models.schemas import Claim
    from app.services.qa_service import _claim_problem

    context = ApprovedContext(
        release_id="r",
        evidence_ids={"e"},
        knowledge_units=[
            {
                "id": "u",
                "action": "Inspect housing",
                "evidence_ids": ["e"],
                "field_evidence": {"action": ["e"]},
                "review_status": "approved",
            }
        ],
    )
    claim = Claim(text="Inspect housing", field="action", knowledge_unit_id="u", evidence_ids=["e"])
    evidence = {
        "e": {
            "task_id": task,
            "type": "video_segment",
            "media_id": "m",
            "start_ms": start,
            "end_ms": end,
        }
    }
    assert (
        _claim_problem(
            claim, {"u": context.knowledge_units[0]}, evidence, {"m": duration}, "task_001", True
        )
        == reason
    )


def test_bounded_live_context_excludes_omitted_units_and_evidence():
    from app.ai.schemas import ApprovedContext
    from app.models.schemas import AskRequest
    from app.services.qa_service import _live_data

    units = [
        {
            "id": f"u{i}",
            "action": "Inspect housing",
            "cue": "x" * 30000,
            "evidence_ids": [f"e{i}"],
            "field_evidence": {"action": [f"e{i}"]},
            "review_status": "approved",
        }
        for i in range(4)
    ]
    context = ApprovedContext(
        release_id="r",
        knowledge_units=units,
        evidence_ids={f"e{i}" for i in range(4)},
        evidence_items=[{"id": f"e{i}", "content": "source"} for i in range(4)],
    )
    data, selected = _live_data(AskRequest(question="Inspect housing", language="en"), context)
    assert len(json.dumps(data, ensure_ascii=False)) <= 64000
    assert [u.id for u in selected] == ["u0", "u1"]
    assert [e["id"] for e in data["evidence_items"]] == ["e0", "e1"]
    assert len(context.knowledge_units) == 4


def test_prompt_data_mutation_does_not_expand_allowlists(live_client, monkeypatch):
    def reply(data):
        claim = good_claim(data)
        unit = next(u for u in data["knowledge_units"] if u["id"] == claim["knowledge_unit_id"])
        unit["interpretation"] = "INJECTED"
        unit["field_evidence"]["interpretation"] = ["ev_injected"]
        data["evidence_items"].append({"id": "ev_injected", "task_id": "task_001"})
        return {
            "status": "answered",
            "claims": [
                {**claim, "text": "INJECTED", "evidence_ids": ["ev_injected"]},
            ],
        }

    fake_ai(monkeypatch, reply)
    result = ask(live_client, "Why inspect housing?").json()
    assert result["status"] == "insufficient_evidence"
    assert result["claims"] == []
