import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app.ai import knowledge
from app.ai.base import ProviderError
from app.ai.schemas import GapAnalysis
from app.models.entities import Clarification, Draft, Evidence
from app.models.knowledge_ai import KnowledgeAIRun
from app.services.gap_generation_service import select_candidates
from tests.conftest import auth

PATH = "/api/v1/tasks/task_001/clarifications/generate"
LIVE = dict(
    ai_provider="gemini",
    gemini_api_key="k",
    gemini_model="gemini-3-test",
    enable_live_gap_generation=True,
)


def candidate(unit, **overrides):
    return {
        "knowledge_unit_id": unit["id"],
        "gap_type": "missing_cue",
        "missing_information": "The observable signal is not explained.",
        "rationale": "An expert can explain the signal.",
        "evidence_ids": unit["evidence_ids"],
        "suggested_question": f"Which observation guides {unit['id']}?",
        "importance": 4,
        "requires_expert": True,
        **overrides,
    }


def setup(client):
    headers = auth(client, "expert")
    draft = client.get("/api/v1/tasks/task_001/draft", headers=headers).json()
    body = dict(
        draft_id=draft["draft_id"],
        expected_revision=draft["revision"],
        mode="live",
        request_id="generate-1",
    )
    return headers, draft, body


def install(monkeypatch, callback):
    calls = []
    closed = []

    class FakeAI:
        provider = "gemini"
        model_id = "gemini-3-test"

        def __init__(self, settings):
            pass

        def call(self, prompt, schema):
            data = json.loads(prompt.split("\nDATA:\n", 1)[1])
            calls.append(data)
            value = callback(data)
            return GapAnalysis.model_validate(value), {"total_token_count": 10}

        def close(self):
            closed.append(True)

    monkeypatch.setattr(knowledge, "KnowledgeAI", FakeAI)
    return calls, closed


def counts(client):
    with client.app.state.sessionmaker() as session:
        return (
            session.scalar(select(func.count()).select_from(Clarification)),
            session.scalar(select(func.count()).select_from(KnowledgeAIRun)),
        )


def test_live_ranked_contextual_questions_and_replay(make_client, monkeypatch):
    with make_client(**LIVE) as client:
        headers, draft, body = setup(client)
        unit = draft["knowledge_units"][0]
        # Populated but uninformative reasoning must not suppress contextual questions.
        with client.app.state.sessionmaker() as session, session.begin():
            row = session.get(Draft, draft["draft_id"])
            units = deepcopy(row.knowledge_units)
            units[0]["interpretation"] = "Inspect again."
            row.knowledge_units = units

        def respond(data):
            # A second short write transaction succeeds while the provider runs.
            with client.app.state.sessionmaker() as session, session.begin():
                session.scalar(select(Draft.revision))
            return {
                "candidates": [
                    candidate(
                        unit,
                        gap_type=kind,
                        importance=importance,
                        suggested_question=f"What explains {kind}?",
                    )
                    for kind, importance in [
                        ("missing_cue", 5),
                        ("missing_exception", 5),
                        ("unobservable_information", 4),
                        ("beginner_mistake", 3),
                    ]
                ]
            }

        calls, closed = install(monkeypatch, respond)
        before = counts(client)
        response = client.post(PATH, json=body, headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
        assert len(result["questions"]) == 3
        assert [q["gap_type"] for q in result["questions"]] == [
            "missing_cue",
            "missing_exception",
            "unobservable_information",
        ]
        assert result["generation_mode"] == "live"
        assert result["provider"] == "gemini"
        assert client.post(PATH, json=body, headers=headers).json() == result
        assert len(calls) == len(closed) == 1
        assert counts(client) == (before[0] + 3, before[1] + 1)
        with client.app.state.sessionmaker() as session:
            assert session.get(Draft, draft["draft_id"]).revision == 1


@pytest.mark.parametrize("code", ["AI_OUTPUT_INVALID", "PROCESSING_TIMEOUT"])
def test_fallback_receipt_replays_without_model(make_client, monkeypatch, code):
    with make_client(**LIVE) as client:
        headers, _, body = setup(client)

        def fail(data):
            raise ProviderError(code, "AI request failed.", True)

        calls, closed = install(monkeypatch, fail)
        result = client.post(PATH, json=body, headers=headers)
        assert result.status_code == 200
        result = result.json()
        assert result["generation_mode"] == "fallback"
        assert result["provider"] == "fixture"
        assert f"DETERMINISTIC_FALLBACK:{code}" in result["warnings"]
        assert client.post(PATH, json=body, headers=headers).json() == result
        assert len(calls) == len(closed) == 1
        with client.app.state.sessionmaker() as session:
            run = session.get(KnowledgeAIRun, result["generation_id"])
            assert run.error["model_id"] == "gemini-3-test"
            assert run.error["prompt_version"] == "gap_elicitation_v1"
            assert run.output["request"]["mode"] == "live"


def test_failure_without_fallback_leaves_no_rows(make_client, monkeypatch):
    with make_client(**LIVE, enable_deterministic_gap_fallback=False) as client:
        headers, _, body = setup(client)

        def fail(data):
            raise ProviderError("PROCESSING_TIMEOUT", "AI request timed out.", True)

        _, closed = install(monkeypatch, fail)
        before = counts(client)
        response = client.post(PATH, json=body, headers=headers)
        assert response.status_code == 503
        assert response.json()["error"] == {
            "code": "PROCESSING_TIMEOUT",
            "message": "AI request timed out.",
            "details": {"retryable": True},
        }
        assert counts(client) == before
        assert closed == [True]


def test_empty_result_and_stale_replay(make_client, monkeypatch):
    with make_client(**LIVE) as client:
        headers, draft, body = setup(client)
        calls, _ = install(monkeypatch, lambda data: {"candidates": []})
        before = counts(client)
        result = client.post(PATH, json=body, headers=headers).json()
        assert result["questions"] == []
        assert client.post(PATH, json=body, headers=headers).json() == result
        with client.app.state.sessionmaker() as session, session.begin():
            session.get(Draft, draft["draft_id"]).revision = 2
        replay = client.post(PATH, json=body, headers=headers).json()
        assert replay == {**result, "status": "stale"}
        assert replay["revision"] == 1
        assert len(calls) == 1
        assert counts(client) == (before[0], before[1] + 1)


@pytest.mark.parametrize(
    "change", [{"draft_id": "other"}, {"expected_revision": 2}, {"mode": "deterministic"}]
)
def test_conflicting_request_id(make_client, monkeypatch, change):
    with make_client(**LIVE) as client:
        headers, _, body = setup(client)
        calls, _ = install(monkeypatch, lambda data: {"candidates": []})
        assert client.post(PATH, json=body, headers=headers).status_code == 200
        result = client.post(PATH, json={**body, **change}, headers=headers)
        assert result.status_code == 409
        assert result.json()["error"]["code"] == "REQUEST_ID_CONFLICT"
        assert len(calls) == 1


@pytest.mark.parametrize("drift", ["revision", "disposition", "evidence"])
def test_snapshot_changes_during_call_leave_no_writes(make_client, monkeypatch, drift):
    with make_client(**LIVE) as client:
        headers, draft, body = setup(client)

        def respond(data):
            with client.app.state.sessionmaker() as session, session.begin():
                if drift == "revision":
                    session.get(Draft, draft["draft_id"]).revision += 1
                elif drift == "disposition":
                    session.get(Clarification, "clar_001").status = "premise_rejected"
                else:
                    session.get(
                        Evidence, data["evidence_items"][0]["id"]
                    ).content = "Changed source"
            return {"candidates": [candidate(data["units"][0])]}

        install(monkeypatch, respond)
        before = counts(client)
        response = client.post(PATH, json=body, headers=headers)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "GENERATION_STALE"
        assert counts(client) == before


def test_initial_stale_revision_disabled_and_roles(client, monkeypatch):
    headers, _, body = setup(client)
    calls, _ = install(monkeypatch, lambda data: {"candidates": []})
    assert client.post(PATH, json=body, headers=headers).status_code == 501
    stale = client.post(PATH, json={**body, "expected_revision": 9}, headers=headers)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"
    assert client.post(PATH, json=body, headers=auth(client, "apprentice")).status_code == 403
    assert calls == []


def test_candidate_reference_validation_and_prior_dispositions():
    units = [
        {"id": "u1", "action": "Inspect", "evidence_ids": ["e1", "other-task"]},
        {"id": "u2", "action": "Inspect", "evidence_ids": ["e2"]},
    ]
    candidates = [
        candidate(units[0], knowledge_unit_id="unknown"),
        candidate(units[0], evidence_ids=["other-task"]),
        candidate(units[0], evidence_ids=["e2"]),
        candidate(units[0], evidence_ids=["e1", "other-task"]),
        candidate(units[0], evidence_ids=[]),
        candidate(units[0], evidence_ids=["e1"], requires_expert=False),
        candidate(units[0], evidence_ids=["e1"], suggested_question=" "),
    ]
    selected, warnings = select_candidates(candidates, units, [{"id": "e1"}, {"id": "e2"}], [])
    assert selected == []
    assert "CANDIDATE_UNKNOWN_UNIT" in warnings
    assert "CANDIDATE_INVALID_EVIDENCE" in warnings
    for disposition in ("answered", "premise_rejected", "unobservable", "deferred"):
        selected, _ = select_candidates(
            [candidate(units[1])],
            units,
            [{"id": "e2"}],
            [{"knowledge_unit_id": "u2", "gap_type": "missing_cue", "status": disposition}],
        )
        assert selected == []


def test_duplicates_and_stable_ties():
    units = [{"id": "u1", "evidence_ids": ["e1"]}, {"id": "u2", "evidence_ids": ["e1"]}]
    candidates = [
        candidate(units[1], suggested_question="Same question?"),
        candidate(units[0], suggested_question=" SAME   QUESTION? "),
        candidate(units[0], gap_type="missing_exception", suggested_question="Other?"),
    ]
    selected, _ = select_candidates(candidates, units, [{"id": "e1"}], [])
    assert [(r["knowledge_unit_id"], r["gap_type"]) for r in selected] == [
        ("u1", "missing_cue"),
        ("u1", "missing_exception"),
    ]


def test_bounded_context_rejects_omitted_records(make_client, monkeypatch):
    with make_client(**LIVE) as client:
        headers, draft, body = setup(client)
        with client.app.state.sessionmaker() as session, session.begin():
            row = session.get(Draft, draft["draft_id"])
            unit = row.knowledge_units[0]
            row.knowledge_units = [
                {
                    **deepcopy(unit),
                    "id": f"unit-{i}",
                    "action": "x" * 2000,
                    "cue": "x" * 2000,
                    "interpretation": "x" * 2000,
                    "decision": "x" * 2000,
                    "exception": "x" * 2000,
                }
                for i in range(35)
            ]

        def respond(data):
            assert len(data["units"]) < 30
            assert len(data["evidence_items"]) <= 60
            assert len(json.dumps(data, ensure_ascii=False)) <= 64000
            assert all(len(unit["action"]) == 1000 for unit in data["units"])
            return {"candidates": [candidate({**unit, "id": "unit-34"})]}

        install(monkeypatch, respond)
        result = client.post(PATH, json=body, headers=headers).json()
        assert result["questions"] == []
        assert "CONTEXT_TRUNCATED" in result["warnings"]
        assert "CANDIDATE_UNKNOWN_UNIT" in result["warnings"]


def test_concurrent_identical_requests_return_first_receipt(make_client, monkeypatch):
    with make_client(**LIVE) as client:
        headers, _, body = setup(client)
        barrier = Barrier(2)

        def respond(data):
            barrier.wait(timeout=10)
            return {"candidates": [candidate(data["units"][0])]}

        calls, _ = install(monkeypatch, respond)
        before = counts(client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(lambda _: client.post(PATH, json=body, headers=headers), range(2))
            )
        assert [response.status_code for response in responses] == [200, 200]
        assert responses[0].json() == responses[1].json()
        assert len(calls) == 2
        assert counts(client) == (before[0] + 1, before[1] + 1)


def test_deterministic_generation_and_logging_disabled(make_client):
    with make_client(enable_ai_eval_logging=False) as client:
        headers, _, body = setup(client)
        response = client.post(PATH, json={**body, "mode": "deterministic"}, headers=headers)
        assert response.status_code == 200
        result = response.json()
        assert result["generation_mode"] == "deterministic"
        assert result["provider"] == "fixture"
        with client.app.state.sessionmaker() as session:
            run = session.get(KnowledgeAIRun, result["generation_id"])
            assert run.usage is run.latency_ms is None


def test_live_logging_disabled_still_keeps_receipt(make_client, monkeypatch):
    with make_client(**LIVE, enable_ai_eval_logging=False) as client:
        headers, _, body = setup(client)
        install(monkeypatch, lambda data: {"candidates": []})
        result = client.post(PATH, json=body, headers=headers).json()
        with client.app.state.sessionmaker() as session:
            run = session.get(KnowledgeAIRun, result["generation_id"])
            assert run.usage is run.latency_ms is None
            assert run.output["response"]["questions"] == []
