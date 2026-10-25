import json
from typing import Literal

from sqlalchemy import delete, select

from app.ai.fixture_provider import FIXTURE_PATH, FixtureAIProvider
from app.ai.schemas import MediaReference, TaskContext
from app.core.security import CurrentUser
from app.database import utcnow
from app.learning.models import Enrollment, ManualCompletion
from app.models.analysis import StudioAnalysisRun
from app.models.entities import (
    Clarification,
    Draft,
    DraftRevision,
    Evidence,
    MediaAsset,
    ProcessingJob,
    Release,
    Task,
)
from app.models.knowledge_ai import KnowledgeAIRun
from app.models.schemas import ClarificationRespond, PublishRequest
from app.models.studio import (
    StudioAudit,
    StudioAuthorization,
    StudioCourseContent,
    StudioEvaluation,
)
from app.services import clarification_service, publication_service

FIXTURE_TASK_ID = "task_001"


def seed_demo(
    session, state: Literal["fresh", "approved_baseline"] = "fresh", provider=None
) -> bool:
    if session.get(Task, FIXTURE_TASK_ID) is not None:
        return False
    fixture = json.loads(FIXTURE_PATH.read_text())
    task_data = fixture["task"]
    provider = provider or FixtureAIProvider()
    context = TaskContext(
        task_id=task_data["id"],
        title=task_data["title"],
        equipment=task_data["equipment"],
        language=task_data["language"],
    )
    extraction = provider.extract_knowledge(
        MediaReference(fixture_id=fixture["fixture_id"]), context
    )
    task = Task(**task_data, status="draft", created_at=utcnow(), updated_at=utcnow())
    session.add(task)
    session.flush()
    for item in extraction.evidence_items:
        session.add(Evidence(**item.model_dump(), task_id=task.id, created_at=utcnow()))
    units = [unit.model_dump() for unit in extraction.knowledge_units]
    draft = Draft(
        id=fixture["draft"]["id"],
        task_id=task.id,
        revision=1,
        status="draft",
        knowledge_units=units,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(draft)
    session.flush()
    session.add(
        DraftRevision(
            draft_id=draft.id,
            revision=1,
            knowledge_units=units,
            actor_id="system",
            reason="seed",
            created_at=utcnow(),
        )
    )
    for index, gap in enumerate(
        provider.generate_clarifications(extraction.knowledge_units, context), 1
    ):
        session.add(
            Clarification(
                id=f"clar_{index:03}",
                task_id=task.id,
                draft_id=draft.id,
                **gap.model_dump(),
                status="pending",
                created_at=utcnow(),
            )
        )
    session.flush()
    if state == "approved_baseline":
        example = fixture["example_expert_responses"][0]
        clarification_service.respond(
            session,
            example["clarification_id"],
            ClarificationRespond(
                expected_draft_revision=draft.revision,
                disposition=example["disposition"],
                answer=example["answer"],
                knowledge_update=example["knowledge_update"],
            ),
            CurrentUser("expert_001", "expert", example["author_label"]),
            evidence_id=example["evidence_id"],
            source_label="illustrative",
        )
        publication_service.publish(
            session,
            task.id,
            PublishRequest(
                expected_revision=draft.revision,
                approval_note=fixture["baseline_release"]["approval_note"],
            ),
            CurrentUser("reviewer_001", "reviewer", "Demo Reviewer"),
            release_id=fixture["baseline_release"]["release_id"],
        )
    session.flush()
    return True


def reset_demo(session, state: Literal["fresh", "approved_baseline"] = "fresh") -> list[str]:
    storage_keys = list(
        session.scalars(select(MediaAsset.storage_key).where(MediaAsset.task_id == FIXTURE_TASK_ID))
    )
    # Learning is currently scoped to this demo course; purge every user and context.
    session.execute(delete(ManualCompletion))
    session.execute(delete(Enrollment))
    draft_ids = select(Draft.id).where(Draft.task_id == FIXTURE_TASK_ID)
    session.execute(delete(DraftRevision).where(DraftRevision.draft_id.in_(draft_ids)))
    for model in (
        KnowledgeAIRun,
        StudioAnalysisRun,
        StudioCourseContent,
        StudioAudit,
        StudioAuthorization,
        StudioEvaluation,
        Clarification,
        Release,
        Draft,
        Evidence,
        ProcessingJob,
        MediaAsset,
    ):
        session.execute(delete(model).where(model.task_id == FIXTURE_TASK_ID))
    session.execute(delete(Task).where(Task.id == FIXTURE_TASK_ID))
    session.flush()
    seed_demo(session, state)
    return storage_keys
