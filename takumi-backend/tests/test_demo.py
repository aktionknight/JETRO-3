import pytest
from sqlalchemy import func, select

from app.models.entities import Draft, DraftRevision, Evidence, Release, Task
from app.services.seed_service import reset_demo, seed_demo
from tests.conftest import auth

RESET = {"scope": "full_demo", "confirm": "RESET_DEMO_DATA", "state": "fresh"}


@pytest.mark.parametrize(
    "role,body,status,code",
    [
        ("expert", RESET, 403, "FORBIDDEN"),
        ("reviewer", {"scope": "full_demo"}, 422, "CONFIRMATION_REQUIRED"),
        ("reviewer", {"scope": "attempts"}, 400, "RESET_SCOPE_UNAVAILABLE"),
    ],
)
def test_reset_guards(client, role, body, status, code):
    response = client.post("/api/v1/demo/reset", headers=auth(client, role), json=body)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_reset_disabled(make_client):
    with make_client(enable_demo_reset=False) as client:
        response = client.post("/api/v1/demo/reset", headers=auth(client, "reviewer"), json=RESET)
        assert response.status_code == 501
        assert response.json()["error"]["code"] == "FEATURE_DISABLED"


def assert_baseline(client):
    headers = auth(client, "apprentice")
    response = client.get("/api/v1/tasks/task_001/release", headers=headers)
    assert response.status_code == 200
    release = response.json()
    assert release["id"] == "release_001" and release["status"] == "active"
    unit = release["knowledge_units"][0]
    for field in ("cue", "interpretation", "decision"):
        assert "ev_002" in unit["field_evidence"][field]
    evidence = client.get("/api/v1/evidence/ev_002", headers=headers).json()
    assert evidence["source_label"] == "illustrative"
    assert evidence["author"] == "Illustrative Example Expert"
    answer = client.post(
        "/api/v1/tasks/task_001/ask",
        headers=headers,
        json={"question": "Why do you inspect the bearing housing?"},
    ).json()
    assert answer["status"] == "answered"
    assert any("ev_002" in claim["evidence_ids"] for claim in answer["claims"])


def test_baseline_reset(client):
    response = client.post(
        "/api/v1/demo/reset",
        headers=auth(client, "reviewer"),
        json={**RESET, "state": "approved_baseline"},
    )
    assert response.status_code == 200
    assert response.json() == {"success": True, "scope": "full_demo", "state": "approved_baseline"}
    assert_baseline(client)


def test_baseline_startup_and_idempotency(make_client):
    with make_client(demo_seed_state="approved_baseline") as client:
        assert_baseline(client)
        with client.app.state.sessionmaker() as session, session.begin():
            assert seed_demo(session, "approved_baseline") is False
            assert seed_demo(session, "approved_baseline") is False
            assert session.scalar(select(func.count()).select_from(Release)) == 1


def test_user_task_survives_reset(client):
    headers = auth(client, "reviewer")
    response = client.post(
        "/api/v1/tasks", headers=headers, json={"title": "User task", "equipment": "User bench"}
    )
    assert response.status_code == 201
    task_id = response.json()["id"]
    for state in ("approved_baseline", "fresh"):
        response = client.post(
            "/api/v1/demo/reset", headers=headers, json={**RESET, "state": state}
        )
        assert response.status_code == 200
        assert client.get(f"/api/v1/tasks/{task_id}", headers=headers).status_code == 200


def test_baseline_failure_rolls_back_whole_reset(client, monkeypatch):
    headers = auth(client, "reviewer")
    before = client.get("/api/v1/tasks/task_001/draft", headers=headers).json()
    from app.services import publication_service

    def fail(*args, **kwargs):
        raise RuntimeError("publication failed")

    monkeypatch.setattr(publication_service, "publish", fail)
    with client.app.state.sessionmaker() as session:
        with pytest.raises(RuntimeError), session.begin():
            reset_demo(session, "approved_baseline")
    with client.app.state.sessionmaker() as session:
        assert session.get(Task, "task_001").active_release_id is None
        assert session.scalar(select(func.count()).select_from(Release)) == 0
        assert session.get(Draft, "draft_001").revision == before["revision"]
        assert session.get(Draft, "draft_001").knowledge_units == before["knowledge_units"]
        assert session.scalar(select(func.count()).select_from(DraftRevision)) == 1
    assert client.get("/api/v1/tasks/task_001/draft", headers=headers).json() == before


@pytest.mark.parametrize(
    "initial_state,target_state,old_revision,new_revision",
    [
        ("fresh", "fresh", 1, 2),
        ("fresh", "approved_baseline", 1, 3),
        ("approved_baseline", "fresh", 2, 3),
        ("approved_baseline", "approved_baseline", 2, 4),
    ],
)
def test_reset_revisions_reject_cached_writes(
    make_client, initial_state, target_state, old_revision, new_revision
):
    with make_client(demo_seed_state=initial_state) as client:
        headers = auth(client, "reviewer")
        path = "/api/v1/tasks/task_001/draft"
        assert client.get(path, headers=headers).json()["revision"] == old_revision
        for _ in range(2):
            response = client.post(
                "/api/v1/demo/reset", headers=headers, json={**RESET, "state": target_state}
            )
            assert response.status_code == 200
            current = client.get(path, headers=headers).json()
            assert current["draft_id"] == "draft_001"
            assert current["task_id"] == "task_001"
            assert current["revision"] == new_revision
            assert [row["id"] for row in current["clarifications"]] == [
                "clar_001", "clar_002", "clar_003"
            ]
            seeded_revision = old_revision + 1
            with client.app.state.sessionmaker() as session:
                history = session.scalars(
                    select(DraftRevision).order_by(DraftRevision.revision)
                ).all()
                expected = (
                    [seeded_revision, new_revision]
                    if target_state == "approved_baseline" else [new_revision]
                )
                assert [row.revision for row in history] == expected
                assert history[-1].knowledge_units == current["knowledge_units"]
                assert history[0].knowledge_units[0]["interpretation"] is None
                release = session.get(Release, "release_001")
                if target_state == "approved_baseline":
                    assert release.source_draft_revision == new_revision
                else:
                    assert release is None
                evidence_ids = list(session.scalars(select(Evidence.id).order_by(Evidence.id)))
                history_count = len(history)
            payload = {
                "expected_revision": old_revision,
                "changes": [{"knowledge_unit_id": "ku_001", "fields": {"action": "Recheck"}}],
            }
            stale = client.patch(path, headers=headers, json=payload)
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "REVISION_CONFLICT"
            assert stale.json()["error"]["details"] == {
                "expected": old_revision, "actual": new_revision
            }
            with client.app.state.sessionmaker() as session:
                assert session.get(Draft, "draft_001").knowledge_units == current["knowledge_units"]
                assert list(session.scalars(select(Evidence.id).order_by(Evidence.id))) == evidence_ids
                assert session.scalar(select(func.count()).select_from(DraftRevision)) == history_count
            updated = client.patch(
                path, headers=headers, json={**payload, "expected_revision": new_revision}
            )
            assert updated.status_code == 200
            assert updated.json()["revision"] == new_revision + 1
            old_revision = new_revision + 1
            new_revision = old_revision + (2 if target_state == "approved_baseline" else 1)


@pytest.mark.parametrize("state,revision", [("fresh", 1), ("approved_baseline", 2)])
def test_reset_without_fixture_starts_revision_sequence(make_client, state, revision):
    with make_client(enable_demo_seed=False) as client:
        headers = auth(client, "reviewer")
        response = client.post(
            "/api/v1/demo/reset", headers=headers, json={**RESET, "state": state}
        )
        assert response.status_code == 200
        draft = client.get("/api/v1/tasks/task_001/draft", headers=headers).json()
        assert draft["draft_id"] == "draft_001"
        assert draft["revision"] == revision
