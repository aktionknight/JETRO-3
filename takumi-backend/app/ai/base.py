from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Protocol

from fastapi import Depends

from app.ai.schemas import (
    ApprovedContext,
    ClarificationCandidate,
    ExtractionResult,
    MediaReference,
    ProviderFile,
    QAProviderResult,
    TaskContext,
    VideoExtraction,
)
from app.config import Settings, get_settings
from app.core.errors import AppError
from app.models.schemas import KnowledgeUnit


class KnowledgeAIProvider(Protocol):
    def extract_knowledge(self, media: MediaReference, task: TaskContext) -> ExtractionResult: ...

    def generate_clarifications(
        self, knowledge_units: list[KnowledgeUnit], task: TaskContext
    ) -> list[ClarificationCandidate]: ...

    def answer_question(
        self, question: str, language: str, context: ApprovedContext
    ) -> QAProviderResult: ...


def get_ai_provider(settings) -> KnowledgeAIProvider:
    from app.ai.fixture_provider import FixtureAIProvider

    # Deterministic provider for seed, extraction gaps and fixture Q&A. Explicit live text
    # operations (gap elicitation, grounded Q&A) are orchestrated by services via KnowledgeAI.
    if settings.ai_provider in ("fixture", "gemini", "ollama", "openrouter"):
        return FixtureAIProvider()
    raise AppError("AI_PROVIDER_UNAVAILABLE", "Configured AI provider is not available.", 503)


def ai_provider(settings: Annotated[Settings, Depends(get_settings)]) -> KnowledgeAIProvider:
    return get_ai_provider(settings)


class ProviderError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class VideoExtractor(Protocol):
    provider: str
    model_id: str

    def check_model(self) -> None: ...

    def upload_file(self, path: Path, mime_type: str, display_name: str) -> ProviderFile: ...

    def wait_until_active(self, name: str, on_poll: Callable[[], None]) -> ProviderFile: ...

    def extract_video(
        self, file: ProviderFile, task: TaskContext, duration_ms: int, has_audio: bool
    ) -> VideoExtraction: ...

    def delete_file(self, name: str) -> None: ...


def get_video_extractor(settings) -> VideoExtractor:
    if settings.ai_provider == "ollama":
        from app.ai.ollama_provider import OllamaVideoExtractor

        return OllamaVideoExtractor(settings)
    if settings.ai_provider == "openrouter":
        from app.ai.openrouter_provider import OpenRouterVideoExtractor

        return OpenRouterVideoExtractor(settings)

    from app.ai.gemini_provider import GeminiAIProvider

    return GeminiAIProvider(settings)
