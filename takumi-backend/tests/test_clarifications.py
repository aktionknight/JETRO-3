from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app.models.entities import Clarification, Draft, Evidence
from app.services import knowledge_service
from tests.conftest import auth

PATH = "/api/v1/clarifications/clar_001/respond"
UPDATE = {
    "knowledge_unit_id": "ku_001",
    "fields": {
        "cue": "Observed leak",
        "interpretation": "Possible seal damage",
        "decision": "Stop inspection",
    },
}


def test_answer_updates_knowledge_and_evidence(client):
    headers = auth(client, "expert")
    response = client.post(
        PATH,
        headers=headers,
        json={
            "expected_draft_revision": 1,
            "disposition": "answered",
            "answer": "A leak may indicate seal damage. Stop inspection.",
            "knowledge_update": UPDATE,
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["draft_revision"] == 2
    assert result["status"] == "answered"
    evidence_id = result["evidence_id"]
    evidence = client.get(f"/api/v1/evidence/{evidence_id}", headers=headers).json()
    assert evidence["type"] == "expert_response"
    assert evidence["author"] == "Demo Expert"
    assert evidence["source_label"] == "real"
    draft = client.get("/api/v1/tasks/task_001/draft", headers=headers).json()
    unit = draft["knowledge_units"][0]
    for field, value in UPDATE["fields"].items():
        assert unit[field] == value
        assert unit["field_evidence"][field] == [evidence_id]
    assert draft["clarifications"][0]["status"] == "answered"
    with client.app.state.sessionmaker() as session:
        assert session.get(Evidence, evidence_id).content == evidence["content"]
    assert (
        client.post(
            PATH,
            headers=headers,
            json={
                "expected_draft_revision": 2,
                "disposition": "deferred",
            },
        ).status_code
        == 409
    )


@pytest.mark.parametrize("disposition", ["premise_rejected", "unobservable", "deferred"])
def test_other_dispositions_preserve_knowledge(client, disposition):
    headers = auth(client, "expert")
    before = client.get("/api/v1/tasks/task_001/draft", headers=headers).json()
    response = client.post(
        PATH,
        headers=headers,
        json={
            "expected_draft_revision": 1,
            "disposition": disposition,
            "answer": "Cannot confirm premise.",
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["draft_revision"] == 1
    assert result["status"] == disposition
    assert result["evidence_id"] is not None
    after = client.get("/api/v1/tasks/task_001/draft", headers=headers).json()
    assert before["knowledge_units"] == after["knowledge_units"]
    assert after["clarifications"][0]["status"] == disposition


@pytest.mark.parametrize(
    "payload",
    [
        {"disposition": "answered", "answer": " "},
        {"disposition": "deferred", "knowledge_update": UPDATE},
        {
            "disposition": "answered",
            "answer": "x",
            "knowledge_update": {"knowledge_unit_id": "ku_002", "fields": {"cue": "x"}},
        },
        {
            "disposition": "answered",
            "answer": "x",
            "knowledge_update": {"knowledge_unit_id": "ku_001", "fields": {}},
        },
    ],
)
def test_validation_before_revision_and_writes(client, payload):
    response = client.post(
        PATH,
        headers=auth(client, "expert"),
        json={
            "expected_draft_revision": 99,
            **payload,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_KNOWLEDGE_UPDATE"
    with client.app.state.sessionmaker() as session:
        assert session.scalar(select(func.count()).select_from(Evidence)) == 3
        assert session.get(Clarification, "clar_001").status == "pending"


def test_missing_stale_and_roles(client):
    headers = auth(client, "expert")
    payload = {"expected_draft_revision": 2, "disposition": "answered", "answer": "x"}
    response = client.post(PATH, headers=headers, json=payload)
    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"expected": 2, "actual": 1}
    response = client.post("/api/v1/clarifications/missing/respond", headers=headers, json=payload)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CLARIFICATION_NOT_FOUND"
    assert client.post(PATH, headers=auth(client, "apprentice"), json=payload).status_code == 403


def test_answer_without_update_links_evidence(client):
    headers = auth(client, "reviewer")
    response = client.post(
        PATH,
        headers=headers,
        json={
            "expected_draft_revision": 1,
            "disposition": "answered",
            "answer": "Confirmed context.",
        },
    )
    assert response.status_code == 200
    draft = client.get("/api/v1/tasks/task_001/draft", headers=headers).json()
    unit = draft["knowledge_units"][0]
    assert response.json()["evidence_id"] in unit["evidence_ids"]
    assert unit["interpretation"] is None



def test_same_text_confirmation_restores_action_support_and_allows_approval(client):
    headers = auth(client, "reviewer")
    draft_path = "/api/v1/tasks/task_001/draft"
    with client.app.state.sessionmaker() as session, session.begin():
        draft = session.get(Draft, "draft_001")
        units = deepcopy(draft.knowledge_units)
        action = units[0]["action"]
        units[0]["field_evidence"].pop("action")
        draft.knowledge_units = units
    rejected = client.patch(
        draft_path,
        headers=headers,
        json={
            "expected_revision": 1,
            "changes": [{"knowledge_unit_id": "ku_001", "operation": "approve"}],
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["details"]["fields"] == ["action"]
    response = client.post(
        PATH,
        headers=headers,
        json={
            "expected_draft_revision": 1,
            "disposition": "answered",
            "answer": "I confirm this action exactly as written.",
            "knowledge_update": {"knowledge_unit_id": "ku_001", "fields": {"action": action}},
        },
    )
    assert response.status_code == 200
    evidence_id = response.json()["evidence_id"]
    unit = client.get(draft_path, headers=headers).json()["knowledge_units"][0]
    assert unit["action"] == action
    assert unit["field_evidence"]["action"] == [evidence_id]
    assert unit["evidence_ids"].count(evidence_id) == 1
    assert unit["review_status"] == "draft"
    approved = client.patch(
        draft_path,
        headers=headers,
        json={
            "expected_revision": 2,
            "changes": [{"knowledge_unit_id": "ku_001", "operation": "approve"}],
        },
    )
    assert approved.status_code == 200
    unit = client.get(draft_path, headers=headers).json()["knowledge_units"][0]
    assert unit["review_status"] == "approved"


def test_failure_rolls_back_every_write(make_client, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("Injected draft failure")

    monkeypatch.setattr(knowledge_service, "bump_draft", fail)
    with make_client(raise_server_exceptions=False) as client:
        response = client.post(
            PATH,
            headers=auth(client, "expert"),
            json={
                "expected_draft_revision": 1,
                "disposition": "answered",
                "answer": "x",
                "knowledge_update": UPDATE,
            },
        )
        assert response.status_code == 500
        with client.app.state.sessionmaker() as session:
            assert session.scalar(select(func.count()).select_from(Evidence)) == 3
            assert session.get(Clarification, "clar_001").status == "pending"
            assert session.scalar(select(Draft)).revision == 1
