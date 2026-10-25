import json

import pytest

from app.ai import knowledge
from scripts.v3_flow import main, run_flow
from tests.conftest import auth

LIVE = dict(
    demo_seed_state="approved_baseline",
    ai_provider="gemini",
    gemini_api_key="offline-test-key",
    gemini_model="gemini-3-test",
    enable_live_gap_generation=True,
    enable_live_qa=True,
)
ILLUSTRATIVE_ANSWER = "Illustrative synthetic: a visible missing base mounting fastener."


def install_fake(monkeypatch):
    calls = []

    class FakeKnowledgeAI:
        model_id = "gemini-3-test"

        def __init__(self, settings):
            pass

        def call(self, prompt, schema):
            data = json.loads(prompt.split("\nDATA:\n", 1)[1])
            calls.append(schema.__name__)
            if schema.__name__ == "GapAnalysis":
                unit = next(u for u in data["units"] if u["id"] == "ku_003")
                result = {
                    "candidates": [
                        {
                            "knowledge_unit_id": "ku_003",
                            "gap_type": "missing_cue",
                            "evidence_ids": unit["evidence_ids"],
                            "importance": 4,
                            "missing_information": "Observable fastener cue is absent.",
                            "rationale": "Illustrative cue needs an expert explanation.",
                            "suggested_question": (
                                "Which visible cue guides the base mounting fastener check?"
                            ),
                            "requires_expert": True,
                        }
                    ]
                }
            else:
                unit = next(u for u in data["knowledge_units"] if u["id"] == "ku_003")
                result = {
                    "status": "answered",
                    "claims": [
                        {
                            "knowledge_unit_id": unit["id"],
                            "field": "cue",
                            "text": unit["cue"],
                            "evidence_ids": unit["field_evidence"]["cue"],
                        }
                    ],
                }
            return schema.model_validate(result), None

        def close(self):
            pass

    monkeypatch.setattr(knowledge, "KnowledgeAI", FakeKnowledgeAI)
    return calls


def test_generate_only_prints_sources_without_answering(make_client, monkeypatch, tmp_path):
    calls = install_fake(monkeypatch)
    with make_client(**LIVE) as client:
        output = []
        result = run_flow(
            client,
            task_id="task_001",
            request_id="generate-only",
            generate_only=True,
            receipt_file=tmp_path / "receipt.json",
            emit=output.append,
        )
        assert len(result["questions"]) == 1
        metadata = json.loads(output[0])
        item = json.loads(output[1])
        assert metadata["generation_mode"] == "live"
        assert item["knowledge_unit_id"] == "ku_003"
        assert item["action"] == "Check that the base mounting fasteners are present"
        assert item["evidence_items"][0]["start_ms"] == 52000
        assert item["evidence_items"][0]["content"]
        assert item["rationale"] and item["missing_information"]
        assert calls == ["GapAnalysis"]
        draft = client.get("/api/v1/tasks/task_001/draft", headers=auth(client, "expert")).json()
        assert next(u for u in draft["knowledge_units"] if u["id"] == "ku_003")["cue"] is None
        assert (
            next(q for q in draft["clarifications"] if q["id"] == item["id"])["status"] == "pending"
        )


@pytest.mark.parametrize("already_answered", [False, True])
def test_full_resume_reuses_original_receipt_after_revision_change(
    make_client, monkeypatch, tmp_path, already_answered
):
    calls = install_fake(monkeypatch)
    with make_client(**LIVE) as client:
        receipt = tmp_path / "receipt.json"
        generation = run_flow(
            client,
            task_id="task_001",
            request_id="resume",
            generate_only=True,
            receipt_file=receipt,
            emit=lambda _: None,
        )
        original = json.loads(receipt.read_text())["request"]
        selected = generation["questions"][0]
        if already_answered:
            headers = auth(client, "expert")
            response = client.post(
                f"/api/v1/clarifications/{selected['id']}/respond",
                headers=headers,
                json={
                    "expected_draft_revision": original["expected_revision"],
                    "disposition": "answered",
                    "answer": ILLUSTRATIVE_ANSWER,
                    "knowledge_update": {
                        "knowledge_unit_id": "ku_003",
                        "fields": {"cue": ILLUSTRATIVE_ANSWER},
                    },
                },
            )
            assert response.status_code == 200, response.text
        output = []
        result = run_flow(
            client,
            task_id="task_001",
            request_id="resume",
            receipt_file=receipt,
            answer=ILLUSTRATIVE_ANSWER,
            question="What signs at the base mounting fasteners should I notice?",
            emit=output.append,
        )
        assert result["generation"]["generation_id"] == generation["generation_id"]
        assert result["generation"]["revision"] == original["expected_revision"]
        assert result["generation"]["status"] == ("stale" if already_answered else "completed")
        assert result["evidence_id"]
        assert calls == ["GapAnalysis", "GroundedAnswer"]
        assert output[-1] == "OK 8 unsupported-torque-abstention"
        assert json.loads(receipt.read_text())["request"] == original


def test_no_new_question_fails_without_fabricated_answer(make_client, monkeypatch, tmp_path):
    class Empty:
        model_id = "gemini-3-test"

        def __init__(self, settings):
            pass

        def call(self, prompt, schema):
            return schema.model_validate({"candidates": []}), None

        def close(self):
            pass

    monkeypatch.setattr(knowledge, "KnowledgeAI", Empty)
    with (
        make_client(**LIVE) as client,
        pytest.raises(RuntimeError, match="No new clarification requires an expert answer."),
    ):
        run_flow(
            client,
            task_id="task_001",
            request_id="empty",
            generate_only=True,
            receipt_file=tmp_path / "receipt.json",
            emit=lambda _: None,
        )


def test_cli_requires_genuine_answer_and_question():
    with pytest.raises(SystemExit) as error:
        main(["--task-id", "task_001"])
    assert error.value.code == 2
