from types import SimpleNamespace

from app.learning import live_evaluation, live_interview, service
from tests.conftest import auth
from tests.test_learning import actions


def test_combined_score_requires_both_assessments_and_preserves_critical_error():
    row = SimpleNamespace(
        playground={
            "scores": {"procedure": 60, "judgment": 90, "knowledge": 90},
            "critical_error": True,
        },
        interview={
            "status": "completed",
            "result": {
                "explanation_points": 3,
                "available_points": 5,
            },
        },
    )
    result = service.final_evaluation(row)
    assert result["playground_score"] == 80
    assert result["interview_score"] == 60
    assert result["overall_score"] == 70
    assert result["critical_playground_error"] is True
    row.playground = None
    assert service.final_evaluation(row)["overall_score"] is None
    row.interview["result"]["available_points"] = 0
    assert service.final_evaluation(row)["interview_score"] is None


def test_live_interview_saves_report_and_progress_returns_scores(make_client, monkeypatch):
    async def generate(*args):
        return "Explain the approved observation and your next decision."

    async def speech(*args):
        return "", ""

    async def evaluate(http, key, model, release, playground, history, duration):
        assert client.app.state.engine.pool.checkedout() == 0
        turns = live_evaluation.answer_turns(history)
        return live_evaluation.build_record(
            release,
            playground,
            turns,
            live_evaluation.Assessment(
                summary="Review the demonstrated limits.",
                grades=[
                    live_evaluation.Grade(
                        answer_index=0,
                        assessable=True,
                        points=4,
                        feedback="Reasoning supported by the approved source.",
                        evidence_ids=[release["evidence_items"][0]["id"]],
                    ),
                ],
            ),
            duration,
        )

    monkeypatch.setattr(live_interview, "_generate_text", generate)
    monkeypatch.setattr(live_interview, "_synthesize_speech", speech)
    monkeypatch.setattr(live_evaluation, "evaluate", evaluate)
    with make_client(demo_seed_state="approved_baseline", gemini_api_key="test-key") as client:
        headers = auth(client, "apprentice")
        assert (
            client.post(
                "/api/v1/learning/complete", headers=headers, json={"stage": "course"}
            ).status_code
            == 200
        )
        played = client.post(
            "/api/v1/learning/playground",
            headers=headers,
            json={
                "scenario": "standard",
                "mode": "guided",
                "actions": actions(),
            },
        )
        assert played.status_code == 200, played.text
        token = headers["Authorization"].split()[1]
        with client.websocket_connect(f"/api/v1/learning/interview/live?token={token}") as ws:
            while ws.receive_json().get("status") != "listening":
                pass
            ws.send_json({"type": "user_turn", "text": "I check the approved evidence first."})
            while ws.receive_json().get("status") != "listening":
                pass
            ws.send_json({"type": "end"})
            while ws.receive_json().get("status") != "ended":
                pass
        saved = client.get("/api/v1/learning/progress", headers=headers).json()
        assert saved["interview"]["status"] == "completed"
        assert saved["interview"]["generation_method"] == "live_ai_assessment"
        assert len(saved["interview"]["answers"]) == 1
        assert saved["evaluation"]["interview_score"] == 80
        scores = list(played.json()["playground_result"]["scores"].values())
        assert saved["evaluation"]["overall_score"] == round(
            (sum(scores) / len(scores) + 80) / 2, 1
        )
        reloaded = client.get("/api/v1/learning/progress", headers=headers).json()
        assert reloaded["evaluation"] == saved["evaluation"]


def test_no_answers_does_not_assign_an_interview_score():
    report = live_evaluation.build_record({"release_id": "r1"}, None, [], None, 2)
    assert report["result"]["available_points"] == 0
    assert report["result"]["assessment_status"] == "unavailable"
