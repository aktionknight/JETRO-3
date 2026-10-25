from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core import security
from app.database import utcnow
from tests.conftest import auth


def test_defaults():
    settings = Settings(_env_file=None)
    assert settings.ai_provider == "fixture"
    for name in type(settings).model_fields:
        if name.startswith(("enable_live_", "enable_media_")):
            assert getattr(settings, name) is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"ai_provider": "gemini"},
            "AI_PROVIDER=gemini requires GEMINI_API_KEY and GEMINI_MODEL",
        ),
        (
            {"ai_provider": "ollama", "ollama_model": "qwen2.5:7b"},
            "AI_PROVIDER=ollama requires OLLAMA_BASE_URL, OLLAMA_MODEL and WHISPER_BASE_URL",
        ),
        (
            {"ai_provider": "openrouter", "openrouter_model": "provider/model"},
            "AI_PROVIDER=openrouter requires OPENROUTER_API_KEY, OPENROUTER_MODEL "
            "and WHISPER_BASE_URL",
        ),
        (
            {"enable_live_extraction": True},
            "ENABLE_LIVE_EXTRACTION requires AI_PROVIDER=gemini, ollama or openrouter "
            "and ENABLE_MEDIA_PROCESSING=true",
        ),
        (
            {"enable_media_processing": True},
            "ENABLE_MEDIA_PROCESSING requires ENABLE_MEDIA_UPLOAD=true",
        ),
        (
            {"enable_processing_worker": True},
            "ENABLE_PROCESSING_WORKER requires ENABLE_MEDIA_PROCESSING=true",
        ),
        (
            {"processing_job_lease_seconds": 120},
            "PROCESSING_JOB_LEASE_SECONDS must exceed PROCESSING_PROVIDER_TIMEOUT_SECONDS",
        ),
        ({"enable_embeddings": True}, "reserved for Checkpoint 6"),
        ({"enable_live_qa": True}, "ENABLE_LIVE_QA requires AI_PROVIDER=gemini"),
        (
            {
                "ai_provider": "gemini",
                "gemini_api_key": "k",
                "gemini_model": "gemini-3-m",
                "enable_live_qa": True,
                "enable_qa_abstention": False,
            },
            "ENABLE_LIVE_QA requires ENABLE_QA_SUPPORT_VALIDATION and ENABLE_QA_ABSTENTION",
        ),
        ({"app_env": "production"}, "must be false when APP_ENV=production"),
        ({"demo_mode": False}, "requires DEMO_MODE=true"),
    ],
)
def test_invalid_settings(overrides, message):
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **overrides)


def test_gemini_settings():
    settings = Settings(
        _env_file=None, ai_provider="gemini", gemini_api_key="k", gemini_model="gemini-3-m"
    )
    assert settings.ai_provider == "gemini"


def test_gemini_model_rejects_non_gemini_3():
    with pytest.raises(ValidationError, match="GEMINI_MODEL must be a Gemini 3-series model id"):
        Settings(_env_file=None, gemini_model="gemini-2.5-flash")


def test_studio_model_rejects_non_gemini_3():
    with pytest.raises(ValidationError, match="STUDIO_AI_MODEL must be a Gemini 3-series model id"):
        Settings(_env_file=None, studio_ai_model="gemini-flash-latest")


def test_openrouter_settings():
    settings = Settings(
        _env_file=None,
        ai_provider="openrouter",
        openrouter_api_key="secret",
        openrouter_model="provider/model",
        whisper_base_url="http://whisper.test",
    )
    assert settings.ai_provider == "openrouter"


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "secret" not in response.text
    assert "gemini_api_key" not in response.text
    assert response.headers["X-Request-ID"]
    assert client.get("/openapi.json").status_code == 200


def test_disabled_login(make_client):
    with make_client(enable_demo_auth=False) as client:
        response = client.post("/api/v1/auth/demo-login", json={"demo_user": "expert"})
        assert response.status_code == 501
        assert response.json()["error"]["code"] == "FEATURE_DISABLED"


def test_reserved_media_auth(client):
    response = client.post(
        "/api/v1/tasks/task_001/media",
        headers=auth(client, "expert"),
        files={"file": ("test.mp4", b"test", "video/mp4")},
    )
    assert response.status_code == 501
    assert response.json()["error"]["details"]["flag"] == "ENABLE_MEDIA_UPLOAD"
    response = client.post("/api/v1/tasks/task_001/media")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert (
        client.post("/api/v1/tasks/task_001/media", headers=auth(client, "apprentice")).status_code
        == 403
    )
    for method, path in [
        ("post", "/api/v1/tasks/task_001/process"),
        ("get", "/api/v1/jobs/missing"),
        ("get", "/api/v1/media/missing"),
    ]:
        assert getattr(client, method)(path).status_code == 401
        kwargs = (
            {"json": {"media_id": "missing", "mode": "live", "request_id": "test"}}
            if method == "post"
            else {}
        )
        assert (
            getattr(client, method)(path, headers=auth(client, "expert"), **kwargs).status_code
            == 501
        )
    assert client.get("/api/v1/media/missing/stream").status_code == 501


def test_tampered_token(client):
    headers = auth(client, "expert")
    headers["Authorization"] += "x"
    response = client.post("/api/v1/tasks/task_001/media", headers=headers)
    assert response.status_code == 401


def test_expired_token(client, monkeypatch):
    past = utcnow() - timedelta(days=2)
    monkeypatch.setattr(security, "utcnow", lambda: past)
    token, _ = security.issue_token(
        "expert", client.app.state.settings, client.app.state.signing_key
    )
    monkeypatch.undo()
    response = client.post(
        "/api/v1/tasks/task_001/media", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


def test_error_envelopes_and_body_limit(client):
    assert client.patch("/api/v1/health").json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    response = client.post("/api/v1/auth/demo-login", json={"demo_user": "invalid"})
    assert response.status_code == 422
    assert "input" not in response.json()["error"]["details"]["errors"][0]
    response = client.post(
        "/api/v1/auth/demo-login",
        content="x",
        headers={"Content-Length": str(1024 * 1024 + 1), "X-Request-ID": "test-id"},
    )
    assert response.status_code == 413
    assert response.headers["X-Request-ID"] == "test-id"
