import asyncio
import logging

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect
from app.learning import live_evaluation, live_interview
from tests.test_learning import actions

from tests.conftest import auth


def test_live_interview_requires_token(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/v1/learning/interview/live"):
            pass
    assert exc.value.code == 4001


def test_live_interview_rejects_invalid_token(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/v1/learning/interview/live?token=invalid_token"):
            pass
    assert exc.value.code == 4001


def test_live_interview_accepts_apprentice_token(client):
    token = auth(client, "apprentice")["Authorization"].split()[1]
    try:
        with client.websocket_connect(f"/api/v1/learning/interview/live?token={token}") as ws:
            msg = ws.receive_json()
            assert msg.get("type") in ("status", "error")
    except WebSocketDisconnect as exc:
        assert exc.code in (4003, 4004)


def unlock_interview(client):
    headers = auth(client, "apprentice")
    assert client.post(
        "/api/v1/learning/complete", headers=headers, json={"stage": "course"}
    ).status_code == 200
    played = client.post(
        "/api/v1/learning/playground",
        headers=headers,
        json={"scenario": "standard", "mode": "guided", "actions": actions()},
    )
    assert played.status_code == 200, played.text
    return headers, headers["Authorization"].split()[1]


@pytest.mark.parametrize("kind", ["text", "speech"])
def test_provider_auth_uses_header_without_logging_credentials(kind, caplog):
    key = "fake-provider-key-must-not-leak"
    requests = []

    def reject(request):
        requests.append(request)
        return httpx.Response(403, json={"error": "Forbidden"})

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                if kind == "text":
                    await live_interview._generate_text(client, key, "fake-model", "Prompt", [])
                else:
                    await live_interview._synthesize_speech(client, key, "Hello")

    with caplog.at_level(logging.INFO):
        asyncio.run(call())
    assert len(requests) == 1
    assert requests[0].headers["x-goog-api-key"] == key
    assert not requests[0].url.query
    assert key not in str(requests[0].url)
    assert key not in caplog.text


@pytest.mark.parametrize("failure", ["http", "malicious_exception"])
def test_opening_failure_is_private(make_client, monkeypatch, caplog, failure):
    key = "fake-opening-key-must-not-leak"
    requests = []
    original_client = httpx.AsyncClient

    def reject(request):
        requests.append(request)
        return httpx.Response(403, text=f"Provider body contains {key}")

    def mock_client(*args, **kwargs):
        return original_client(*args, transport=httpx.MockTransport(reject), **kwargs)

    if failure == "http":
        monkeypatch.setattr(live_interview.httpx, "AsyncClient", mock_client)
    else:
        async def fail(*args):
            raise RuntimeError(f"https://provider.invalid/?key={key} private response body")

        monkeypatch.setattr(live_interview, "_generate_text", fail)

    with caplog.at_level(logging.INFO):
        with make_client(demo_seed_state="approved_baseline", gemini_api_key=key) as client:
            _, token = unlock_interview(client)
            with client.websocket_connect(f"/api/v1/learning/interview/live?token={token}") as ws:
                assert ws.receive_json() == {"type": "status", "status": "connecting"}
                assert ws.receive_json() == {
                    "type": "error",
                    "message": "Interview unavailable. Please try again.",
                }
                with pytest.raises(WebSocketDisconnect):
                    ws.receive_json()
    if failure == "http":
        assert len(requests) == 1
        assert requests[0].headers["x-goog-api-key"] == key
        assert not requests[0].url.query
    assert key not in caplog.text
    assert "private response body" not in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize(
    ("stage", "failure"),
    [
        ("initial_tts", "malicious_exception"),
        ("initial_tts", 429),
        ("initial_tts", 403),
        ("turn_tts", "malicious_exception"),
        ("turn_tts", 429),
        ("turn_tts", 403),
        ("turn_generation", "malicious_exception"),
    ],
)
def test_recoverable_provider_errors_do_not_expose_exception_text(
    make_client, monkeypatch, caplog, stage, failure
):
    key = "fake-recoverable-key-must-not-leak"
    generated = 0
    spoken = 0
    events = []

    async def generate(*args):
        nonlocal generated
        generated += 1
        if stage == "turn_generation" and generated == 2:
            raise RuntimeError(f"https://provider.invalid/?key={key} private response body")
        return "Explain the approved observation."

    async def speech(*args):
        nonlocal spoken
        spoken += 1
        if (stage == "initial_tts" and spoken == 1) or (
            stage == "turn_tts" and spoken == 2
        ):
            if isinstance(failure, int):
                request = httpx.Request("POST", f"https://provider.invalid/?key={key}")
                response = httpx.Response(
                    failure, request=request, text=f"private response body {key}"
                )
                raise httpx.HTTPStatusError(
                    f"private response body {key}", request=request, response=response
                )
            raise RuntimeError(f"https://provider.invalid/?key={key} private response body")
        return "", ""

    async def evaluate(http, key, model, release, playground, history, duration):
        return live_evaluation.build_record(
            release, playground, live_evaluation.answer_turns(history), None, duration
        )

    monkeypatch.setattr(live_evaluation, "evaluate", evaluate)

    monkeypatch.setattr(live_interview, "_generate_text", generate)
    monkeypatch.setattr(live_interview, "_synthesize_speech", speech)
    with caplog.at_level(logging.INFO):
        with make_client(demo_seed_state="approved_baseline", gemini_api_key=key) as client:
            _, token = unlock_interview(client)
            with client.websocket_connect(f"/api/v1/learning/interview/live?token={token}") as ws:
                while True:
                    events.append(ws.receive_json())
                    if events[-1].get("status") == "listening":
                        break
                if stage != "initial_tts":
                    ws.send_json({"type": "user_turn", "text": "I inspect the approved evidence."})
                    while True:
                        events.append(ws.receive_json())
                        if events[-1].get("status") == "listening":
                            break
                # End normally so the socket closes after persistence.
                ws.send_json({"type": "end"})
                while True:
                    events.append(ws.receive_json())
                    if events[-1].get("status") == "ended":
                        break
    expected_message = (
        "Gemini voice quota or rate limit reached (429). Check the TTS model quota "
        "in Google AI Studio for this API key's project. Text questions still work."
        if failure == 429
        else "Gemini could not generate interviewer audio. Check the voice service configuration."
    )
    voice_errors = [event for event in events if event.get("type") == "voice_error"]
    assert voice_errors == (
        [] if stage == "turn_generation" else [{"type": "voice_error", "message": expected_message}]
    )
    assert key not in str(events)
    assert "private response body" not in str(events)
    assert ("HTTPStatusError" if isinstance(failure, int) else "RuntimeError") in caplog.text
    assert key not in caplog.text
    assert "private response body" not in caplog.text
    assert "Traceback" not in caplog.text
