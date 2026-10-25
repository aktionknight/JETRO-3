"""Explicit, task-scoped Studio reset. Caller owns the transaction."""

from sqlalchemy import delete, select

from app.core.errors import AppError
from app.database import utcnow
from app.learning.models import Enrollment, ManualCompletion
from app.models.analysis import StudioAnalysisRun
from app.models.entities import (
    Clarification,
    DraftRevision,
    Evidence,
    MediaAsset,
    ProcessingJob,
    Release,
    Task,
)
from app.models.knowledge_ai import KnowledgeAIRun
from app.models.studio import (
    StudioAudit,
    StudioAuthorization,
    StudioCourseContent,
    StudioEvaluation,
)
from app.services import knowledge_service


def reset(session, task_id, expected_revision):
    task = session.get(Task, task_id)
    if task is None:
        raise AppError("TASK_NOT_FOUND", "Task not found.", 404)
    draft = knowledge_service.get_draft(session, task_id)
    knowledge_service.check_revision(session, draft, expected_revision)
    if session.scalar(
        select(StudioAnalysisRun.id).where(
            StudioAnalysisRun.task_id == task_id, StudioAnalysisRun.status == "running"
        )
    ) or session.scalar(
        select(ProcessingJob.id).where(
            ProcessingJob.task_id == task_id, ProcessingJob.status.in_(["queued", "running"])
        )
    ):
        raise AppError("RESET_BUSY", "Wait for task processing to finish before resetting.", 409)
    storage_keys = list(
        session.scalars(select(MediaAsset.storage_key).where(MediaAsset.task_id == task_id))
    )
    release_ids = select(Release.id).where(Release.task_id == task_id)
    for enrollment in session.scalars(
        select(Enrollment).where(Enrollment.release_id.in_(release_ids))
    ).all():
        session.execute(
            delete(ManualCompletion).where(
                ManualCompletion.user_id == enrollment.user_id,
                ManualCompletion.context == enrollment.context,
            )
        )
        session.delete(enrollment)
    session.flush()
    task.active_release_id = None
    task.status = "draft"
    task.updated_at = utcnow()
    session.flush()
    if saved := session.get(StudioCourseContent, task_id):
        # Keep the version monotonic so pre-reset tabs cannot overwrite post-reset sources.
        saved.script_text = saved.script_filename = saved.review_notes = ""
        saved.version += 1
        saved.updated_at = utcnow()
    for model in (
        KnowledgeAIRun,
        StudioAnalysisRun,
        StudioAudit,
        StudioAuthorization,
        StudioEvaluation,
        Clarification,
        Release,
        Evidence,
        ProcessingJob,
        MediaAsset,
    ):
        session.execute(delete(model).where(model.task_id == task_id))
    session.execute(delete(DraftRevision).where(DraftRevision.draft_id == draft.id))
    draft.knowledge_units = []
    draft.status = "draft"
    draft.revision += 1  # Older tabs cannot write into the reset workspace.
    draft.updated_at = utcnow()
    session.flush()
    return storage_keys
