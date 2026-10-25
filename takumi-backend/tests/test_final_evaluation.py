import asyncio
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from app.learning import live_evaluation, live_interview, service
from app.learning.models import Enrollment
from tests.conftest import auth
from tests.test_learning import actions
from tests.test_live_interview import unlock_interview


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
        assert saved["interview"]["transcript"] == [{
            "question": "Explain the approved observation and your next decision.",
            "answer": "I check the approved evidence first.",
        }]
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


@pytest.mark.parametrize(
    "delivery",
    [
        "user_ack", "thinking", "examiner_text", "examiner_audio", "fallback",
        "evaluating", "ended", "assessment_unavailable", "timer", "voice_error",
    ],
)
@pytest.mark.parametrize(
    "failure", ["os_error", "disconnect", "disconnected_runtime", "client_disconnected_runtime"]
)
def test_outbound_transport_loss_still_saves_accepted_answer(
    make_client, monkeypatch, delivery, failure
):
    answer = "I check the approved evidence first."
    original_send = WebSocket.send_json
    accepted = False
    failed = False
    sends_after_failure = []
    generated = 0
    evaluated = []
    timer_failed = asyncio.Event()

    async def generate(*args):
        nonlocal generated
        assert client.app.state.engine.pool.checkedout() == 0
        generated += 1
        if generated == 2 and delivery == "fallback":
            raise ValueError("Provider unavailable")
        if generated == 2 and delivery == "timer":
            await asyncio.wait_for(timer_failed.wait(), timeout=5)
        return "Opening question." if generated == 1 else "Examiner follow-up."

    async def speech(*args):
        assert client.app.state.engine.pool.checkedout() == 0
        if generated == 2 and delivery == "voice_error":
            raise ValueError("Provider speech unavailable")
        return "ZmFrZQ==", "audio/L16;codec=pcm;rate=24000"

    async def evaluate(http, key, model, release, playground, history, duration):
        assert client.app.state.engine.pool.checkedout() == 0
        assert not any(
            task is not asyncio.current_task()
            and task.get_coro().__qualname__.endswith("timer_task")
            for task in asyncio.all_tasks()
        )
        turns = live_evaluation.answer_turns(history)
        evaluated.append(turns)
        if delivery == "assessment_unavailable":
            raise ValueError("Assessment unavailable")
        return live_evaluation.build_record(
            release,
            playground,
            turns,
            live_evaluation.Assessment(
                summary="Grounded answer.",
                grades=[
                    live_evaluation.Grade(
                        answer_index=0,
                        assessable=True,
                        points=4,
                        feedback="Approved evidence identified.",
                        evidence_ids=[release["evidence_items"][0]["id"]],
                    )
                ],
            ),
            duration,
        )

    async def send(ws, payload, *args, **kwargs):
        nonlocal accepted, failed
        if failed:
            sends_after_failure.append(payload)
        if payload.get("role") == "user":
            accepted = True
        target = (
            (delivery == "user_ack" and payload.get("role") == "user")
            or (delivery == "thinking" and payload.get("status") == "thinking")
            or (
                delivery in ("examiner_text", "assessment_unavailable")
                and payload.get("text") == "Examiner follow-up."
            )
            or (delivery == "examiner_audio" and accepted and payload.get("type") == "audio")
            or (delivery == "voice_error" and accepted and payload.get("type") == "voice_error")
            or (
                delivery == "fallback"
                and payload.get("text") == "Thank you. Let us continue with the next check."
            )
            or (delivery in ("evaluating", "ended") and payload.get("status") == delivery)
            or (delivery == "timer" and accepted and payload.get("type") == "timer")
        )
        if target:
            failed = True
            timer_failed.set()
            if failure == "os_error":
                raise OSError("Outbound transport lost")
            if failure == "disconnect":
                raise WebSocketDisconnect(code=1006)
            if failure == "client_disconnected_runtime":
                ws.client_state = WebSocketState.DISCONNECTED
            else:
                ws.application_state = WebSocketState.DISCONNECTED
            raise RuntimeError("Cannot send after disconnect")
        return await original_send(ws, payload, *args, **kwargs)

    monkeypatch.setattr(live_interview, "_generate_text", generate)
    monkeypatch.setattr(live_interview, "_synthesize_speech", speech)
    monkeypatch.setattr(live_evaluation, "evaluate", evaluate)
    monkeypatch.setattr(WebSocket, "send_json", send)
    # Keep the ASGI transport open for its final close frame; only its state reports loss.
    original_close = WebSocket.close

    async def close(ws, *args, **kwargs):
        if failed and ws.application_state == WebSocketState.DISCONNECTED:
            ws.application_state = WebSocketState.CONNECTED
        await original_close(ws, *args, **kwargs)

    monkeypatch.setattr(WebSocket, "close", close)
    with make_client(demo_seed_state="approved_baseline", gemini_api_key="fake-key") as client:
        headers, token = unlock_interview(client)
        with client.websocket_connect(f"/api/v1/learning/interview/live?token={token}") as ws:
            while ws.receive_json().get("status") != "listening":
                pass
            ws.send_json({"type": "user_turn", "text": answer})
            if delivery in ("evaluating", "ended"):
                while ws.receive_json().get("status") != "listening":
                    pass
                ws.send_json({"type": "end"})
            with pytest.raises(WebSocketDisconnect):
                while True:
                    ws.receive_json()
        saved = client.get("/api/v1/learning/progress", headers=headers).json()
        report = saved["interview"]
        with client.app.state.sessionmaker() as session:
            persisted = session.scalar(select(Enrollment)).interview
            assert persisted["transcript"] == report["transcript"]
            assert persisted["answers"] == report["answers"]
            assert persisted["result"] == report["result"]
        assert report["status"] == "completed"
        assert report["transcript"] == [{"question": "Opening question.", "answer": answer}]
        if delivery == "assessment_unavailable":
            assert report["result"]["available_points"] == 0
            assert report["result"]["assessment_status"] == "unavailable"
            assert saved["evaluation"]["interview_score"] is None
        else:
            assert report["answers"][0]["selected_answer"] == answer
            assert report["result"]["assessment_status"] == "assessed"
            assert saved["evaluation"]["interview_score"] == 80
    assert failed
    assert sends_after_failure == []
    assert evaluated == [[{"question": "Opening question.", "answer": answer}]]


@pytest.mark.parametrize("assessment_fails", [False, True])
def test_receive_disconnect_saves_transcript_without_inventing_score(
    make_client, monkeypatch, caplog, assessment_fails
):
    answer = "I check the approved evidence first."
    finished = threading.Event()
    original_close = WebSocket.close

    async def generate(*args):
        return "Opening question."

    async def speech(*args):
        return "", ""

    async def evaluate(http, key, model, release, playground, history, duration):
        assert client.app.state.engine.pool.checkedout() == 0
        if assessment_fails:
            raise ValueError("private-provider-secret-must-not-leak")
        return live_evaluation.build_record(
            release, playground, live_evaluation.answer_turns(history), None, duration
        )

    async def close(ws, *args, **kwargs):
        try:
            await original_close(ws, *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(live_interview, "_generate_text", generate)
    monkeypatch.setattr(live_interview, "_synthesize_speech", speech)
    monkeypatch.setattr(live_evaluation, "evaluate", evaluate)
    monkeypatch.setattr(WebSocket, "close", close)
    with make_client(demo_seed_state="approved_baseline", gemini_api_key="fake-key") as client:
        headers, token = unlock_interview(client)
        with client.websocket_connect(f"/api/v1/learning/interview/live?token={token}") as ws:
            while ws.receive_json().get("status") != "listening":
                pass
            ws.send_json({"type": "user_turn", "text": answer})
            while ws.receive_json().get("status") != "listening":
                pass
            ws.close()
            assert finished.wait(5), "Endpoint did not finalize after receive disconnect"
        report = client.get("/api/v1/learning/progress", headers=headers).json()["interview"]
        assert report["transcript"] == [{"question": "Opening question.", "answer": answer}]
        assert report["result"]["available_points"] == 0
        assert report["result"]["explanation_points"] == 0
        assert report["result"]["assessment_status"] == "unavailable"
    assert "private-provider-secret-must-not-leak" not in caplog.text


def test_live_assessment_does_not_overwrite_a_reset_enrollment(make_client, monkeypatch):
    async def generate(*args):
        return "Opening question."

    async def speech(*args):
        return "", ""

    async def evaluate(http, key, model, release, playground, history, duration):
        assert client.app.state.engine.pool.checkedout() == 0
        with client.app.state.sessionmaker() as session, session.begin():
            row = session.scalar(select(Enrollment))
            service.reset_progress(session, row, client.app.state.settings)
        return live_evaluation.build_record(
            release, playground, live_evaluation.answer_turns(history), None, duration
        )

    monkeypatch.setattr(live_interview, "_generate_text", generate)
    monkeypatch.setattr(live_interview, "_synthesize_speech", speech)
    monkeypatch.setattr(live_evaluation, "evaluate", evaluate)
    with make_client(demo_seed_state="approved_baseline", gemini_api_key="fake-key") as client:
        headers, token = unlock_interview(client)
        with client.websocket_connect(f"/api/v1/learning/interview/live?token={token}") as ws:
            while ws.receive_json().get("status") != "listening":
                pass
            ws.send_json({"type": "user_turn", "text": "I check the approved evidence first."})
            while ws.receive_json().get("status") != "listening":
                pass
            ws.send_json({"type": "end"})
            while True:
                event = ws.receive_json()
                assert event.get("status") != "ended"
                if event["type"] == "error":
                    assert event["message"] == "Interview unavailable. Please try again."
                    break
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        progress = client.get("/api/v1/learning/progress", headers=headers).json()
        assert progress["interview"] is None
        assert progress["playground_result"] is None
        assert progress["evaluation"]["interview_score"] is None
