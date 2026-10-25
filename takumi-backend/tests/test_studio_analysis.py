import json

from app.ai.base import ProviderError
from app.ai.studio_analysis import Critique, Findings, Proposals
from app.services import studio_analysis_service as service
from tests.conftest import auth

BASE = "/api/v1/studio/tasks/task_001/analysis"


def test_studio_model_override_is_used_by_provider_and_configuration(settings_factory):
    from app.ai.studio_analysis import StudioAgents

    settings = settings_factory(
        gemini_api_key="test-key",
        gemini_model="gemini-3-video",
        studio_ai_model="gemini-3-studio",
    )
    agents = StudioAgents(settings)
    try:
        assert agents.model == service.configuration(settings)["model"] == "gemini-3-studio"
    finally:
        agents.client.close()


def test_studio_model_falls_back_to_general_model_then_default(settings_factory):
    assert settings_factory(gemini_model="gemini-3-video").studio_model == "gemini-3-video"
    assert settings_factory().studio_model == "gemini-3.5-flash-lite"


def test_provider_uses_json_schema_transport_for_strict_outputs(settings_factory, monkeypatch):
    from types import SimpleNamespace

    from app.ai.studio_analysis import StudioAgents

    agents = StudioAgents(settings_factory(gemini_api_key="test-key"))

    def generate(**kwargs):
        config = kwargs["config"]
        assert config.response_schema is None
        assert config.thinking_config.thinking_level == "LOW"
        assert config.response_json_schema["additionalProperties"] is False
        assert config.response_json_schema["$defs"]["Finding"]["additionalProperties"] is False
        # Gemini returns 400 INVALID_ARGUMENT for the nested Findings array limits.
        assert "maxItems" not in json.dumps(config.response_json_schema)
        return SimpleNamespace(text='{"findings": []}')

    monkeypatch.setattr(agents.client.models, "generate_content", generate)
    try:
        assert agents.extract("Recorded source", []).findings == []
    finally:
        agents.client.close()


def test_workspace_reads_connected_task_evidence_and_requires_explicit_rehearsal(make_client):
    from app.models.entities import Evidence, Task

    with make_client(demo_seed_state="approved_baseline") as client:
        headers = auth(client, "admin")
        with client.app.state.sessionmaker() as session, session.begin():
            task = session.get(Task, "task_001")
            task.title = "Current instructor task"
            evidence = session.get(Evidence, "ev_001")
            evidence.content = "Current recorded instructor source, not a preset proposal."
        workspace = client.get("/api/v1/studio/workspace", headers=headers).json()
        assert workspace["overview"]["task"]["title"] == "Current instructor task"
        assert workspace["user"]["role"] == "admin"
        config = workspace["analysis"]
        assert "Current recorded instructor source" in config["source_notes"]
        assert config["source_notes"] != service.demo_source()["source_notes"]
        assert config["mode"] == "unconfigured"
        response = client.post(BASE, headers=headers, json={"expected_revision": 2})
        assert response.json()["error"]["code"] == "AI_KEY_REQUIRED"
        assert client.get(BASE + "/latest", headers=headers).status_code == 404


def test_live_custom_source_generates_dynamic_patches(make_client, monkeypatch):
    source = "Ask the instructor to clarify the recorded observation."

    class DynamicAgents:
        def __init__(self, settings):
            pass

        def extract(self, notes, units):
            assert notes == source
            return Findings(findings=[])

        def patch(self, notes, units, findings):
            return Proposals(
                proposals=[
                    {
                        "knowledge_unit_id": units[-1]["id"],
                        "field": "exception",
                        "after": notes,
                        "source_quote": notes,
                        "rationale": "Direct instructor source.",
                    }
                ]
            )

        def critique(self, notes, proposals):
            assert proposals[0]["after"] == source
            return Critique(passed=True, issues=[], summary="Supported by the submitted source.")

    monkeypatch.setattr(service, "StudioAgents", DynamicAgents)
    with make_client(demo_seed_state="approved_baseline", gemini_api_key="test-key") as client:
        headers = auth(client, "admin")
        response = client.post(
            BASE,
            headers=headers,
            json={
                "expected_revision": 2,
                "mode": "live",
                "source_notes": source,
            },
        )
        assert response.status_code == 200
        run = response.json()
        assert run["mode"] == "live"
        assert run["proposals"][0]["after"] == source
        assert run["proposals"][0]["knowledge_unit_id"] == "ku_003"
        assert run["evaluation"]["summary"] == "Supported by the submitted source."


def revision(client, headers):
    return client.get("/api/v1/tasks/task_001/draft", headers=headers).json()["revision"]


def diagnose(client, headers):
    config = client.get(BASE + "/config", headers=headers).json()
    return client.post(
        BASE,
        headers=headers,
        json={
            "expected_revision": revision(client, headers),
            "mode": "live" if config["mode"] == "live" else "rehearsal",
            "source_notes": service.demo_source()["source_notes"],
        },
    )


def approve(client, headers, run, **extra):
    return client.post(
        f"{BASE}/{run['id']}/publish",
        headers=headers,
        json={
            "expected_revision": run["revision"],
            "proposal_ids": [p["id"] for p in run["proposals"]],
            "approval_note": "Reviewed illustrative source; keep unknown criteria unknown.",
            **extra,
        },
    )


def test_rehearsal_proposes_then_human_publishes_to_apprentice(make_client):
    with make_client(demo_seed_state="approved_baseline") as client:
        headers = auth(client, "expert")
        learner = auth(client, "apprentice")
        original = client.get("/api/v1/learning/course", headers=learner).json()
        config = client.get(BASE + "/config", headers=headers).json()
        assert config["mode"] == "unconfigured"
        assert config["rehearsal_available"] is True
        run = diagnose(client, headers).json()
        assert run["status"] == "ready" and run["evaluation"]["passed"]
        assert run["evaluation"]["unresolved_clarifications"]
        assert client.get("/api/v1/learning/course", headers=learner).json() == original
        blocked = approve(client, headers, run)
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "EVALUATION_FAILED"
        published = approve(client, headers, run, defer_unanswered=True)
        assert published.status_code == 200, published.text
        assert published.json()["run"]["status"] == "published"
        updated = client.get("/api/v1/learning/course", headers=learner).json()
        assert updated["release_id"] != original["release_id"]
        assert updated["knowledge_units"][1]["exception"] == run["proposals"][0]["after"]
        assert updated["source_label"] == "illustrative"
        assert approve(client, headers, run, defer_unanswered=True).json() == published.json()
        again = diagnose(client, headers).json()
        assert again["proposals"] == []


def test_custom_source_needs_key_and_apprentice_cannot_diagnose(make_client):
    with make_client(demo_seed_state="approved_baseline") as client:
        headers = auth(client, "expert")
        assert (
            client.post(
                BASE, headers=auth(client, "apprentice"), json={"expected_revision": 2}
            ).status_code
            == 403
        )
        response = client.post(
            BASE, headers=headers, json={"expected_revision": 2, "source_notes": "Custom notes"}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "AI_KEY_REQUIRED"


def test_stale_run_cannot_publish(make_client):
    with make_client(demo_seed_state="approved_baseline") as client:
        headers = auth(client, "expert")
        run = diagnose(client, headers).json()
        response = client.patch(
            "/api/v1/tasks/task_001/draft",
            headers=headers,
            json={
                "expected_revision": run["revision"],
                "changes": [
                    {
                        "knowledge_unit_id": "ku_001",
                        "fields": {"exception": "Unknown pending review"},
                    }
                ],
            },
        )
        assert response.status_code == 200
        response = approve(client, headers, run, defer_unanswered=True)
        assert response.status_code == 409


def test_live_failure_is_persistent_and_does_not_fallback(make_client, monkeypatch):
    class Failing:
        def __init__(self, settings):
            pass

        def extract(self, source, units):
            raise ProviderError(
                "AI_PROVIDER_AUTH_FAILED", "AI provider authentication failed.", False
            )

    monkeypatch.setattr(service, "StudioAgents", Failing)
    with make_client(demo_seed_state="approved_baseline", gemini_api_key="test-key") as client:
        headers = auth(client, "expert")
        assert client.get(BASE + "/config", headers=headers).json()["mode"] == "live"
        run = diagnose(client, headers).json()
        assert run["status"] == "failed" and run["mode"] == "live"
        assert run["proposals"] == []
        assert run["error"]["code"] == "AI_PROVIDER_AUTH_FAILED"
        assert client.get(BASE + "/latest", headers=headers).json()["id"] == run["id"]
        assert approve(client, headers, run).status_code == 422


def test_live_agents_called_outside_database_transaction_and_extracts_only(
    make_client, monkeypatch
):
    calls = []

    class Live:
        def __init__(self, settings):
            pass

        def extract(self, source, units):
            assert client.app.state.engine.pool.checkedout() == 0
            calls.append("extract")
            return Findings(findings=[])

        def patch(self, source, units, findings):
            assert client.app.state.engine.pool.checkedout() == 0
            calls.append("patch")
            return Proposals(proposals=service.demo_source()["proposals"])

        def critique(self, source, proposals):
            assert client.app.state.engine.pool.checkedout() == 0
            calls.append("critique")
            return Critique(passed=True, issues=[], summary="Source supported.")

    monkeypatch.setattr(service, "StudioAgents", Live)
    with make_client(demo_seed_state="approved_baseline", gemini_api_key="test-key") as client:
        headers = auth(client, "expert")
        run = diagnose(client, headers).json()
        assert calls == ["extract", "patch", "critique"]
        assert run["status"] == "ready" and run["mode"] == "live"


def test_source_validation_rejects_unsupported_output():
    import pytest

    p = service.demo_source()["proposals"][0].copy()
    p["after"] = "Unsupported claim"
    with pytest.raises(ProviderError, match="exact source"):
        service.validate_proposals([p], service.demo_source()["source_notes"], [{"id": "ku_002"}])


def test_exact_subquote_keeps_uncertainty_qualifier_and_negation(caplog):
    p = {
        "knowledge_unit_id": "ku_1",
        "field": "interpretation",
        "source_quote": "the seal or bearing is damaged",
        "after": "the seal or bearing is damaged",
        "rationale": "Stated cause.",
    }

    def kept(source, quote=None):
        if quote:
            p["source_quote"] = p["after"] = quote
        [result] = service.validate_proposals([p], source, [{"id": "ku_1"}])
        assert result["after"] == result["source_quote"]
        return result["after"]

    hedged = "If it feels gritty, stop. That usually means the seal or bearing is damaged."
    assert kept(hedged) == "That usually means the seal or bearing is damaged"
    negated = "Roughness does not usually mean the seal or bearing is damaged."
    assert kept(negated) == negated[:-1]
    contracted = "Noise alone doesn't mean the seal or bearing is damaged."
    assert kept(contracted) == contracted[:-1]
    # Qualifiers in an earlier sentence do not leak into an unqualified claim.
    assert kept("It may rattle. If it feels gritty, stop.", "stop") == "stop"
    decimal = "At 0.5 mm, usually replace the shim."
    assert kept(decimal, "replace the shim") == decimal[:-1]
    assert kept("Wipe it. Wipe it.", "Wipe it") == "Wipe it"
    ambiguous = "Do not tighten the collar.\nAt final inspection, tighten the collar."
    p["source_quote"] = p["after"] = "tighten the collar"
    assert service.validate_proposals([p], ambiguous, [{"id": "ku_1"}]) == []
    assert "Dropping ambiguous source quote" in caplog.text
    unambiguous = {**p, "source_quote": "At final inspection", "after": "At final inspection"}
    [result] = service.validate_proposals([p, unambiguous], ambiguous, [{"id": "ku_1"}])
    assert result["after"] == "At final inspection"


def test_interrupted_run_recovery_and_duplicate_lock(make_client):
    from app.database import new_id
    from app.models.analysis import StudioAnalysisRun

    with make_client(demo_seed_state="approved_baseline") as client:
        headers = auth(client, "expert")
        with client.app.state.sessionmaker() as session, session.begin():
            row = StudioAnalysisRun(
                id=new_id("analysis"),
                task_id="task_001",
                revision=2,
                source_hash="test",
                source_notes="test",
                source_label="illustrative",
                mode="live",
                model="test",
                status="running",
                stage="extracting",
                requested_by="expert_001",
                proposals=[],
            )
            session.add(row)
        assert diagnose(client, headers).json()["error"]["code"] == "ANALYSIS_RUNNING"
        with client.app.state.sessionmaker() as session, session.begin():
            service.recover_interrupted(session)
        recovered = client.get(BASE + "/latest", headers=headers).json()
        assert recovered["status"] == "failed"
        assert recovered["error"]["code"] == "ANALYSIS_INTERRUPTED"
        assert diagnose(client, headers).json()["status"] == "ready"


def test_ai_critique_failure_cannot_publish(make_client, monkeypatch):
    class Reject:
        def __init__(self, settings):
            pass

        def extract(self, source, units):
            return Findings(findings=[])

        def patch(self, source, units, findings):
            return Proposals(proposals=service.demo_source()["proposals"])

        def critique(self, source, proposals):
            return Critique(passed=False, issues=["Review relevance"], summary="Needs review.")

    monkeypatch.setattr(service, "StudioAgents", Reject)
    with make_client(demo_seed_state="approved_baseline", gemini_api_key="test-key") as client:
        headers = auth(client, "expert")
        run = diagnose(client, headers).json()
        assert run["evaluation"]["passed"] is False
        response = approve(client, headers, run, defer_unanswered=True)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ANALYSIS_NOT_APPROVABLE"


def test_reviewed_extracts_replace_draft_fields_with_source_evidence_without_publishing(
    make_client,
):
    with make_client(demo_seed_state="approved_baseline") as client:
        headers = auth(client, "expert")
        before = client.get("/api/v1/studio/workspace", headers=headers).json()["overview"]
        run = diagnose(client, headers).json()
        proposal = run["proposals"][0]
        req = {
            "expected_revision": run["revision"],
            "proposal_ids": [proposal["id"]],
            "approval_note": "Checked extracted wording against the recorded source.",
        }
        response = client.post(f"{BASE}/{run['id']}/apply", headers=headers, json=req)
        assert response.status_code == 200, response.text
        assert response.json()["run"]["status"] == "applied"
        after = client.get("/api/v1/studio/workspace", headers=headers).json()["overview"]
        assert after["draft"]["revision"] == before["draft"]["revision"] + 1
        assert after["active_release"] == before["active_release"]
        assert after["draft"]["clarifications"] == before["draft"]["clarifications"]
        unit = next(
            unit
            for unit in after["draft"]["knowledge_units"]
            if unit["id"] == proposal["knowledge_unit_id"]
        )
        assert unit[proposal["field"]] == proposal["after"]
        sources = [
            e for e in after["evidence"] if e["id"] in unit["field_evidence"][proposal["field"]]
        ]
        assert any(
            run["id"] in (e["content"] or "") and proposal["source_quote"] in e["content"]
            for e in sources
        )
        assert all(e["source_label"] == "illustrative" for e in sources)
        assert after["analysis_runs"][0]["id"] == run["id"]
        assert after["analysis_runs"][0]["evaluation"] == run["evaluation"]
        assert after["analysis_runs"][0]["source_notes"] == run["source_notes"]
        assert after["analysis_runs"][0]["status"] == "applied"
        event = next(e for e in after["audit"] if e["action"] == "analysis_saved_to_draft")
        assert event["details"]["proposal_ids"] == req["proposal_ids"]
        assert event["details"]["review_note"] == req["approval_note"]
        # A repeated request cannot silently apply it to another revision.
        assert (
            client.post(f"{BASE}/{run['id']}/apply", headers=headers, json=req).status_code == 409
        )


def test_stale_extracts_cannot_replace_reviewed_draft(make_client):
    with make_client(demo_seed_state="approved_baseline") as client:
        headers = auth(client, "expert")
        run = diagnose(client, headers).json()
        changed = client.patch(
            "/api/v1/tasks/task_001/draft",
            headers=headers,
            json={
                "expected_revision": run["revision"],
                "changes": [
                    {
                        "knowledge_unit_id": run["proposals"][0]["knowledge_unit_id"],
                        "fields": {
                            "exception": "Keep this observation unknown until expert review."
                        },
                    }
                ],
            },
        )
        assert changed.status_code == 200
        result = client.post(
            f"{BASE}/{run['id']}/apply",
            headers=headers,
            json={
                "expected_revision": run["revision"] + 1,
                "proposal_ids": [p["id"] for p in run["proposals"]],
                "approval_note": "Reviewed",
            },
        )
        assert result.status_code == 409
        assert result.json()["error"]["code"] == "ANALYSIS_STALE"
