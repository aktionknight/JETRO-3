from copy import deepcopy

import pytest
from sqlalchemy import select

from app.models.entities import Clarification, Draft, DraftRevision, Evidence, MediaAsset
from app.services.knowledge_service import apply_field_updates
from tests.conftest import auth

PATH = "/api/v1/tasks/task_001/draft"


def patch(client, headers, fields, revision=1, unit="ku_001"):
    return client.patch(
        PATH,
        headers=headers,
        json={
            "expected_revision": revision,
            "changes": [{"knowledge_unit_id": unit, "fields": fields}],
        },
    )


def test_read_and_patch(client):
    headers = auth(client, "reviewer")
    draft = client.get(PATH, headers=headers).json()
    assert draft["revision"] == 1
    assert len(draft["knowledge_units"]) == len(draft["clarifications"]) == 3
    assert [c["id"] for c in draft["clarifications"]] == ["clar_001", "clar_002", "clar_003"]
    response = patch(client, headers, {"action": "Inspect housing visually"})
    assert response.status_code == 200
    result = response.json()
    assert result["revision"] == 2
    evidence_id = result["correction_evidence_ids"][0]
    evidence = client.get(f"/api/v1/evidence/{evidence_id}", headers=headers).json()
    assert evidence["type"] == "reviewer_correction"
    assert evidence["author"] == "Demo Reviewer"
    unit = client.get(PATH, headers=headers).json()["knowledge_units"][0]
    assert unit["action"] == "Inspect housing visually"
    assert evidence_id in unit["field_evidence"]["action"]
    stale = patch(client, headers, {"cue": "x"})
    assert stale.status_code == 409
    assert stale.json()["error"]["details"] == {"expected": 1, "actual": 2}
    with client.app.state.sessionmaker() as session:
        revisions = session.scalars(select(DraftRevision).order_by(DraftRevision.revision)).all()
        assert [row.revision for row in revisions] == [1, 2]
        assert revisions[0].knowledge_units[0]["action"] == "Inspect the bearing housing"


@pytest.mark.parametrize(
    "fields",
    [
        {"evidence_ids": ["ev_fake"]},
        {"review_status": "approved"},
        {},
        {"action": None},
        {"cue": "   "},
    ],
)
def test_reject_invalid_fields(client, fields):
    headers = auth(client, "expert")
    assert patch(client, headers, fields).status_code == 422
    assert client.get(PATH, headers=headers).json()["revision"] == 1


def test_unknown_and_duplicate_units_are_atomic(client):
    headers = auth(client, "expert")
    changes = [
        {"knowledge_unit_id": "ku_001", "fields": {"cue": "new"}},
        {"knowledge_unit_id": "missing", "fields": {"cue": "new"}},
    ]
    result = client.patch(PATH, headers=headers, json={"expected_revision": 1, "changes": changes})
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "KNOWLEDGE_UNIT_NOT_FOUND"
    changes[1] = changes[0]
    result = client.patch(PATH, headers=headers, json={"expected_revision": 1, "changes": changes})
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "VALIDATION_ERROR"
    with client.app.state.sessionmaker() as session:
        assert session.scalar(select(Draft)).revision == 1
    assert client.get(PATH, headers=auth(client, "apprentice")).status_code == 403


def test_clear_and_noop(client):
    headers = auth(client, "expert")
    response = patch(client, headers, {"cue": None}, unit="ku_002")
    assert response.status_code == 200
    unit = client.get(PATH, headers=headers).json()["knowledge_units"][1]
    assert unit["cue"] is None
    assert "cue" not in unit["field_evidence"]
    assert unit["evidence_ids"] == ["ev_003", *response.json()["correction_evidence_ids"]]
    response = patch(client, headers, {"cue": None}, revision=2, unit="ku_002")
    assert response.json()["revision"] == 3
    assert response.json()["correction_evidence_ids"] == []


def test_apply_field_updates_is_pure():
    unit = {
        "action": "x",
        "cue": "y",
        "field_evidence": {"cue": ["old"]},
        "evidence_ids": ["old"],
        "review_status": "draft",
    }
    original = deepcopy(unit)
    result = apply_field_updates(unit, {"cue": "new"}, "ev_new")
    assert unit == original
    assert result["field_evidence"]["cue"] == ["old", "ev_new"]
    assert apply_field_updates(result, {"cue": "newer"}, "ev_new")["evidence_ids"] == [
        "old",
        "ev_new",
    ]


def test_same_text_confirmation_adds_support_without_mutating_input():
    unit = {
        "action": "Inspect",
        "cue": None,
        "field_evidence": {},
        "evidence_ids": ["ev_old"],
        "review_status": "approved",
    }
    original = deepcopy(unit)
    confirmed = apply_field_updates(unit, {"action": "Inspect", "cue": None}, "ev_confirm")
    assert unit == original
    assert confirmed["field_evidence"] == {"action": ["ev_confirm"]}
    assert confirmed["evidence_ids"] == ["ev_old", "ev_confirm"]
    assert confirmed["review_status"] == "draft"
    assert apply_field_updates(confirmed, {"action": "Inspect"}, "ev_confirm") == confirmed
    assert apply_field_updates(unit, {"action": "Inspect"}, None) == original
    assert apply_field_updates(unit, {"cue": None}, "ev_confirm") == original


def test_unchanged_patch_keeps_approval_and_provenance(client):
    headers = auth(client, "reviewer")
    assert review_change(client, headers, operation="approve").status_code == 200
    before = client.get(PATH, headers=headers).json()["knowledge_units"][0]
    with client.app.state.sessionmaker() as session:
        evidence_before = list(session.scalars(select(Evidence.id).order_by(Evidence.id)))
    response = patch(client, headers, {"action": before["action"]}, revision=2)
    assert response.status_code == 200
    assert response.json()["correction_evidence_ids"] == []
    assert client.get(PATH, headers=headers).json()["knowledge_units"][0] == before
    with client.app.state.sessionmaker() as session:
        assert list(session.scalars(select(Evidence.id).order_by(Evidence.id))) == evidence_before


def review_change(client, headers, *, unit="ku_001", revision=1, **change):
    return client.patch(
        PATH,
        headers=headers,
        json={
            "expected_revision": revision,
            "changes": [{"knowledge_unit_id": unit, **change}],
        },
    )


def video_evidence(client, headers):
    unit = client.get(PATH, headers=headers).json()["knowledge_units"][0]
    return next(
        item
        for evidence_id in unit["evidence_ids"]
        if (item := client.get(f"/api/v1/evidence/{evidence_id}", headers=headers).json())["type"]
        == "video_segment"
    )


@pytest.mark.parametrize("with_fields", [False, True])
def test_interval_correction_preserves_history(client, with_fields):
    headers = auth(client, "reviewer")
    original = client.get(PATH, headers=headers).json()["knowledge_units"][0]
    old = video_evidence(client, headers)
    change = {"interval": {"evidence_id": old["id"], "start_ms": 100, "end_ms": 200}}
    if with_fields:
        change["fields"] = {"cue": "Corrected observation"}
    result = review_change(client, headers, **change)
    assert result.status_code == 200
    evidence_id = result.json()["correction_evidence_ids"][0]
    evidence = client.get(f"/api/v1/evidence/{evidence_id}", headers=headers).json()
    assert evidence["type"] == "video_segment"
    assert (evidence["start_ms"], evidence["end_ms"]) == (100, 200)
    assert evidence["media_id"] == old["media_id"]
    assert evidence["source_label"] == old["source_label"]
    assert evidence["author"] == "Demo Reviewer"
    assert (
        evidence["content"]
        == (
            f"[Interval corrected by Demo Reviewer from {old['start_ms']}-{old['end_ms']} ms] "
            f"{old['content'] or ''}"
        ).strip()
    )
    unit = client.get(PATH, headers=headers).json()["knowledge_units"][0]
    assert evidence_id in unit["evidence_ids"]
    assert old["id"] not in unit["evidence_ids"]
    for field, support in original["field_evidence"].items():
        if old["id"] in support:
            assert evidence_id in unit["field_evidence"][field]
    assert all(old["id"] not in support for support in unit["field_evidence"].values())
    assert unit["review_status"] == "draft"
    if with_fields:
        assert unit["cue"] == "Corrected observation"
        assert len(result.json()["correction_evidence_ids"]) == 2
    assert client.get(f"/api/v1/evidence/{old['id']}", headers=headers).json() == old
    with client.app.state.sessionmaker() as session:
        history = session.scalar(select(DraftRevision).where(DraftRevision.revision == 1))
        assert history.knowledge_units[0] == original


@pytest.mark.parametrize("start,end", [(100, 100), (200, 100), (0, 1001)])
def test_interval_correction_rejects_invalid_range(client, start, end):
    headers = auth(client, "expert")
    old = video_evidence(client, headers)
    with client.app.state.sessionmaker() as session, session.begin():
        session.add(
            MediaAsset(
                id="media_interval",
                task_id="task_001",
                owner_id="expert_001",
                original_filename="interval.mp4",
                storage_key="interval.mp4",
                mime_type="video/mp4",
                container="mp4",
                size_bytes=1,
                sha256="0" * 64,
                duration_ms=1000,
                video_codec="h264",
                width=16,
                height=16,
                status="ready",
            )
        )
        session.get(Evidence, old["id"]).media_id = "media_interval"
    response = review_change(
        client, headers, interval={"evidence_id": old["id"], "start_ms": start, "end_ms": end}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_EVIDENCE"
    assert response.json()["error"]["details"] == {"evidence_id": old["id"]}
    assert client.get(PATH, headers=headers).json()["revision"] == 1


def test_interval_correction_rejects_uncited_evidence(client):
    headers = auth(client, "expert")
    draft = client.get(PATH, headers=headers).json()
    unit = draft["knowledge_units"][0]
    evidence_id = next(
        evidence_id
        for other in draft["knowledge_units"][1:]
        for evidence_id in other["evidence_ids"]
        if evidence_id not in unit["evidence_ids"]
    )
    response = review_change(
        client, headers, interval={"evidence_id": evidence_id, "start_ms": 100, "end_ms": 200}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_EVIDENCE"


def test_approve_then_edit_resets_review_status(client):
    headers = auth(client, "reviewer")
    response = review_change(client, headers, operation="approve")
    assert response.status_code == 200
    assert response.json()["correction_evidence_ids"] == []
    assert (
        client.get(PATH, headers=headers).json()["knowledge_units"][0]["review_status"]
        == "approved"
    )
    response = patch(client, headers, {"action": "Inspect bearing housing again"}, revision=2)
    assert response.status_code == 200
    assert (
        client.get(PATH, headers=headers).json()["knowledge_units"][0]["review_status"] == "draft"
    )


@pytest.mark.parametrize("field", ["action", "cue", "interpretation", "decision", "exception"])
def test_approve_rejects_unsupported_field(client, field):
    headers = auth(client, "reviewer")
    with client.app.state.sessionmaker() as session, session.begin():
        draft = session.scalar(select(Draft))
        units = deepcopy(draft.knowledge_units)
        units[0][field] = "Unsupported content"
        units[0]["field_evidence"].pop(field, None)
        draft.knowledge_units = units
    response = review_change(client, headers, operation="approve")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "UNIT_NOT_APPROVABLE"
    assert response.json()["error"]["details"] == {
        "knowledge_unit_id": "ku_001",
        "fields": [field],
    }
    assert client.get(PATH, headers=headers).json()["revision"] == 1


def test_reject_removes_unit_and_resolves_pending_clarifications(client):
    headers = auth(client, "reviewer")
    original = client.get(PATH, headers=headers).json()
    with client.app.state.sessionmaker() as session, session.begin():
        session.get(Clarification, original["clarifications"][0]["id"]).knowledge_unit_id = "ku_003"
    response = review_change(client, headers, operation="reject", unit="ku_003")
    assert response.status_code == 200
    draft = client.get(PATH, headers=headers).json()
    assert [unit["id"] for unit in draft["knowledge_units"]] == ["ku_001", "ku_002"]
    for clarification in draft["clarifications"]:
        if clarification["knowledge_unit_id"] == "ku_003":
            assert clarification["status"] == "premise_rejected"
            assert clarification["answer"] == "Knowledge unit rejected during review."
            assert clarification["answered_at"] is not None
        else:
            assert clarification["status"] == "pending"
    with client.app.state.sessionmaker() as session:
        rows = session.scalars(
            select(Clarification).where(Clarification.knowledge_unit_id == "ku_003")
        ).all()
        assert rows
        assert all(row.answered_by == "reviewer_001" for row in rows)
        history = session.scalar(select(DraftRevision).where(DraftRevision.revision == 1))
        assert history.knowledge_units == original["knowledge_units"]


def test_reject_with_stale_revision_conflicts(client):
    headers = auth(client, "expert")
    assert patch(client, headers, {"cue": "Updated cue"}).status_code == 200
    response = review_change(client, headers, operation="reject", unit="ku_003")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVISION_CONFLICT"
    draft = client.get(PATH, headers=headers).json()
    assert len(draft["knowledge_units"]) == 3
    assert all(item["status"] == "pending" for item in draft["clarifications"])


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"operation": "approve", "fields": {"cue": "Not allowed"}},
        {"operation": "reject", "fields": {"cue": "Not allowed"}},
        {"operation": "approve", "interval": {"evidence_id": "ev_001", "start_ms": 0, "end_ms": 1}},
        {"operation": "reject", "interval": {"evidence_id": "ev_001", "start_ms": 0, "end_ms": 1}},
    ],
)
def test_review_operation_rejects_invalid_payload(client, change):
    headers = auth(client, "reviewer")
    response = review_change(client, headers, **change)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get(PATH, headers=headers).json()["revision"] == 1


def test_reject_all_units_allows_empty_draft(client):
    headers = auth(client, "reviewer")
    draft = client.get(PATH, headers=headers).json()
    response = client.patch(
        PATH,
        headers=headers,
        json={
            "expected_revision": draft["revision"],
            "changes": [
                {"knowledge_unit_id": unit["id"], "operation": "reject"}
                for unit in draft["knowledge_units"]
            ],
        },
    )
    assert response.status_code == 200
    draft = client.get(PATH, headers=headers).json()
    assert draft["knowledge_units"] == []
    assert all(item["status"] == "premise_rejected" for item in draft["clarifications"])
