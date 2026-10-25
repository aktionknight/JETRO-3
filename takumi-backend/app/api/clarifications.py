from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.security import CurrentUser, require_roles
from app.database import get_db
from app.models.schemas import (
    ClarificationGenerate,
    ClarificationGenerateOut,
    ClarificationRespond,
    ClarificationRespondOut,
)
from app.services import clarification_service, gap_generation_service

router = APIRouter(tags=["clarifications"])


@router.post("/clarifications/{clarification_id}/respond", response_model=ClarificationRespondOut)
def respond(
    clarification_id: str,
    req: ClarificationRespond,
    session: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_roles("expert", "reviewer"))],
):
    with session.begin():
        result = clarification_service.respond(session, clarification_id, req, user)
    return result


@router.post("/tasks/{task_id}/clarifications/generate", response_model=ClarificationGenerateOut)
def generate(
    task_id: str,
    req: ClarificationGenerate,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[CurrentUser, Depends(require_roles("expert", "reviewer"))],
):
    return gap_generation_service.generate(
        request.app.state.sessionmaker, settings, task_id, req, user
    )
