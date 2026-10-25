from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.ai.base import KnowledgeAIProvider, ai_provider
from app.config import Settings, get_settings
from app.core.errors import feature_disabled
from app.core.security import CurrentUser, get_current_user
from app.models.schemas import AskOut, AskRequest
from app.services import qa_service

router = APIRouter(tags=["qa"])


@router.post("/tasks/{task_id}/ask", response_model=AskOut)
def ask(
    task_id: str,
    req: AskRequest,
    request: Request,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    provider: Annotated[KnowledgeAIProvider, Depends(ai_provider)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    if not settings.enable_grounded_qa:
        raise feature_disabled("ENABLE_GROUNDED_QA")
    return qa_service.ask(request.app.state.sessionmaker, settings, task_id, req, user, provider)
