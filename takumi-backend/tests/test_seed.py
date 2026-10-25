import json

import pytest
from sqlalchemy import func, select

from app.ai.fixture_provider import FIXTURE_PATH, FixtureAIProvider
from app.ai.schemas import MediaReference, TaskContext
from app.core.errors import AppError
from app.database import init_db
from app.models.entities import Clarification, Draft, DraftRevision, Evidence, Task
from app.models.schemas import KnowledgeUnit
from app.services.gap_detection import detect_gaps
from app.services.seed_service import seed_demo


def test_seed_idempotent(client):
    with client.app.state.sessionmaker() as session, session.begin():
        assert seed_demo(session) is False
        assert seed_demo(session) is False
        for model, count in [(Task, 1), (Evidence, 3), (Clarification, 3), (DraftRevision, 1)]:
            assert session.scalar(select(func.count()).select_from(model)) == count
        draft = session.get(Draft, "draft_001")
        assert draft.revision == 1
        assert len(draft.knowledge_units) == 3
        clarifications = session.scalars(select(Clarification).order_by(Clarification.id)).all()
        assert [(c.id, c.knowledge_unit_id, c.gap_type, c.priority) for c in clarifications] == [
            ("clar_001", "ku_001", "missing_reasoning", 10),
            ("clar_002", "ku_002", "changed_condition", 5),
            ("clar_003", "ku_001", "beginner_mistake", 2),
        ]
        assert [c.question for c in clarifications] == [
            (
                "What observation prompts you to inspect the bearing housing at this point, "
                "and what does it tell you?"
            ),
            (
                "After you rotate the shaft slowly by hand through one full turn, "
                "what observation would make you change your next decision?"
            ),
            (
                "What mistake could a beginner make when they inspect the bearing housing, "
                "and how would you recognize it?"
            ),
        ]


def test_complete_knowledge_has_no_gaps():
    unit = KnowledgeUnit(
        id="ku",
        action="Act",
        cue="cue",
        interpretation="meaning",
        decision="decision",
        exception="exception",
    )
    assert detect_gaps([unit]) == []


def test_fixture_priority_and_fallback():
    fixture = json.loads(FIXTURE_PATH.read_text())
    assert [
        (g.knowledge_unit_id, g.gap_type, g.priority)
        for g in detect_gaps(fixture["draft"]["knowledge_units"])
    ] == [
        ("ku_001", "missing_reasoning", 10),
        ("ku_002", "changed_condition", 5),
        ("ku_001", "beginner_mistake", 2),
    ]
    gaps = detect_gaps([KnowledgeUnit(id="ku", action="")])
    assert gaps[0].question == "What observation prompts this step, and what does it tell you?"


def test_unknown_fixture():
    with pytest.raises(AppError) as exc:
        FixtureAIProvider().extract_knowledge(
            MediaReference(fixture_id="unknown"),
            TaskContext(task_id="task", title="title", equipment="equipment", language="en"),
        )
    assert exc.value.code == "FIXTURE_NOT_AVAILABLE"


def test_existing_fixture_is_not_replaced(client):
    with client.app.state.sessionmaker() as session:
        # Existing fixture remains untouched even when a different state is requested.
        assert seed_demo(session, "approved_baseline") is False


def test_schema_version_guard(client):
    from sqlalchemy import text

    with client.app.state.engine.begin() as connection:
        connection.execute(text("UPDATE app_meta SET value='99' WHERE key='schema_version'"))
    with pytest.raises(RuntimeError, match="back up and delete"):
        init_db(client.app.state.engine)


def test_schema_v1_additive_upgrade(client):
    from sqlalchemy import inspect, text

    with client.app.state.engine.begin() as connection:
        connection.execute(text("DROP TABLE media_assets"))
        connection.execute(text("UPDATE app_meta SET value='1' WHERE key='schema_version'"))
    init_db(client.app.state.engine)
    with client.app.state.engine.begin() as connection:
        assert (
            connection.scalar(text("SELECT value FROM app_meta WHERE key='schema_version'")) == "8"
        )
    tables = inspect(client.app.state.engine).get_table_names()
    assert "media_assets" in tables
    assert "learning_enrollments" in tables
    assert "studio_analysis_runs" in tables
    assert "knowledge_ai_runs" in tables


def test_schema_v7_upgrade_preserves_releases_and_clarifications(make_client):
    from sqlalchemy import inspect, text

    snapshot = "SELECT id, content_hash FROM releases ORDER BY id"
    clarifications = "SELECT id, status, answer FROM clarifications ORDER BY id"
    with make_client(demo_seed_state="approved_baseline") as client:
        engine = client.app.state.engine
        with engine.begin() as connection:
            releases_before = connection.execute(text(snapshot)).all()
            clarifications_before = connection.execute(text(clarifications)).all()
            connection.execute(text("DROP TABLE knowledge_ai_runs"))
            connection.execute(text("UPDATE app_meta SET value='7' WHERE key='schema_version'"))
        init_db(engine)
        with engine.begin() as connection:
            version = connection.scalar(
                text("SELECT value FROM app_meta WHERE key='schema_version'")
            )
            assert version == "8"
            assert releases_before and connection.execute(text(snapshot)).all() == releases_before
            assert connection.execute(text(clarifications)).all() == clarifications_before
        assert "knowledge_ai_runs" in inspect(engine).get_table_names()
