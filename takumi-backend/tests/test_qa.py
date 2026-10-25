from copy import deepcopy

import pytest

from app.ai.base import ai_provider
from app.ai.schemas import ApprovedContext, QAProviderResult
from app.models.entities import Draft, Evidence
from app.services.qa_matching import INSUFFICIENT_MSG, LANGUAGE_MSG, deterministic_answer
from tests.conftest import auth
from tests.test_publication import edit_draft, publish_request


def answer_ku_001(app):
    with app.state.sessionmaker() as session, session.begin():
        session.add(
            Evidence(
                id="ev_test_answer",
                task_id="task_001",
                type="expert_response",
                source_label="real",
                author_id="expert_001",
                author_label="Demo Expert",
                content=(
                    "Grease leaking at the housing seal suggests bearing damage; stop and escalate."
                ),
            )
        )
        draft = session.get(Draft, "draft_001")
        units = deepcopy(draft.knowledge_units)
        fields = {
            "cue": "Grease leaking at the bearing housing seal",
            "interpretation": "Possible bearing damage at the housing seal",
            "decision": "Stop and escalate to a supervisor before rotating the shaft",
        }
        units[0].update(fields)
        units[0]["evidence_ids"].append("ev_test_answer")
        for field in fields:
            units[0]["field_evidence"][field] = ["ev_test_answer"]
        draft.knowledge_units = units
        draft.revision += 1


def ask(client, question, language="en"):
    return client.post(
        "/api/v1/tasks/task_001/ask",
        headers=auth(client, "apprentice"),
        json={"question": question, "language": language},
    )


def test_grounded_answer(client):
    answer_ku_001(client.app)
    release_id = publish_request(client, 2).json()["release_id"]
    response = ask(client, "Why do you inspect the bearing housing?")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "answered"
    assert result["release_id"] == release_id
    assert result["review_required"] is False
    assert result["claims"]
    release = client.get(f"/api/v1/releases/{release_id}", headers=auth(client, "expert")).json()
    ids = {item["id"] for item in release["evidence_items"]}
    for claim in result["claims"]:
        assert claim["knowledge_unit_id"] == "ku_001"
        assert claim["evidence_ids"] == ["ev_test_answer"]
        assert set(claim["evidence_ids"]) <= ids


@pytest.mark.parametrize(
    "question,status",
    [
        ("What is the torque specification for the housing bolts?", "insufficient_evidence"),
        ("Why check the base mounting fasteners?", "insufficient_evidence"),
        ("Why?", "needs_context"),
    ],
)
def test_abstention(client, question, status):
    answer_ku_001(client.app)
    publish_request(client, 2)
    response = ask(client, question)
    assert response.status_code == 200
    assert response.json()["status"] == status
    assert response.json()["claims"] == []
    if "torque" in question:
        assert response.json()["missing_information"] == [INSUFFICIENT_MSG]


def test_ignores_draft_only_reasoning(client):
    answer_ku_001(client.app)
    publish_request(client, 2)
    edit_draft(
        client.app,
        lambda units: units[2].update(interpretation="Base mounting fasteners prevent movement"),
    )
    assert ask(client, "Why check the base mounting fasteners?").json()["status"] == (
        "insufficient_evidence"
    )


def test_no_release(client):
    response = ask(client, "Why inspect the bearing housing?")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NO_ACTIVE_RELEASE"


def test_qa_disabled(make_client):
    with make_client(enable_grounded_qa=False) as client:
        response = ask(client, "Why inspect the bearing housing?")
        assert response.status_code == 501
        assert response.json()["error"]["code"] == "FEATURE_DISABLED"


@pytest.mark.parametrize(
    "unit_id,evidence_ids",
    [
        ("ku_001", ["ev_fake"]),
        ("ku_fake", ["ev_001"]),
        ("ku_001", []),
    ],
)
def test_provider_claim_validation(client, unit_id, evidence_ids):
    publish_request(client)

    class Provider:
        def answer_question(self, question, language, context):
            # Mutating provider input cannot change the server's citation allowlist.
            context.evidence_ids.add("ev_fake")
            return QAProviderResult(
                status="answered",
                provenance={"provider": "stub"},
                claims=[
                    {
                        "text": "Unsupported claim",
                        "knowledge_unit_id": unit_id,
                        "field": "action",
                        "evidence_ids": evidence_ids,
                    }
                ],
            )

    client.app.dependency_overrides[ai_provider] = lambda: Provider()
    response = ask(client, "Why inspect the bearing housing?")
    assert response.status_code == 200
    assert response.json()["status"] == "insufficient_evidence"
    assert response.json()["claims"] == []
    assert response.json()["missing_information"] == [INSUFFICIENT_MSG]


def test_non_english():
    result = deterministic_answer(
        "Why?",
        "ja",
        ApprovedContext(release_id="release_test", knowledge_units=[], evidence_ids=set()),
    )
    assert result.status == "needs_context"
    assert result.missing_information == [LANGUAGE_MSG]


@pytest.mark.parametrize(
    "question,fields",
    [
        ("How rotate the shaft?", ["action"]),
        ("What signs during shaft rotation?", ["cue"]),
        ("What decision after shaft rotation?", ["decision"]),
        ("What mistake while rotating shaft?", []),
    ],
)
def test_matching_intents(client, question, fields):
    publish_request(client)
    result = ask(client, question).json()
    assert [claim["field"] for claim in result["claims"]] == fields


def test_empty_and_unmatched_context():
    context = ApprovedContext(release_id="release_test", knowledge_units=[], evidence_ids=set())
    assert deterministic_answer("Why?", "en", context).status == "needs_context"
    assert (
        deterministic_answer("Unknown component", "en", context).status == "insufficient_evidence"
    )


def test_rank_units_weight_frequency_synonyms_and_stable_ties():
    from app.services.qa_matching import CONTENT_FIELDS, rank_units

    def unit(id, action, cue=""):
        return dict.fromkeys(CONTENT_FIELDS, "") | {"id": id, "action": action, "cue": cue}

    units = [
        unit("common", "bearing", "gage"),
        unit("specific", "gauge identification bolt turn bearing"),
        unit("tie", "gauge identification bolt turn bearing"),
    ]
    assert [u["id"] for u in rank_units("gage id fastener rotate", units)] == [
        "specific",
        "tie",
        "common",
    ]
    assert rank_units("unrelated", units) == []
    assert rank_units("bearing", units, limit=1)[0]["id"] == "common"
    assert rank_units("gauge", [unit("cue", "", "gauge"), unit("action", "gauge")])[0]["id"] == (
        "action"
    )
    rare = [unit("common", "bearing bearing"), unit("rare", "", "wobble")]
    rare += [unit(f"other-{i}", "bearing") for i in range(3)]
    assert rank_units("bearing wobble", rare, limit=1)[0]["id"] == "rare"
