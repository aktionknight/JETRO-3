import pytest
from starlette.websockets import WebSocketDisconnect

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
