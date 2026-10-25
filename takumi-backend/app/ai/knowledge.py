"""Structured Gemini text calls for live gap elicitation and grounded Q&A. No database access."""

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.ai.base import ProviderError
from app.ai.gemini_provider import _normalize

_MODEL_MISSING = ("AI_MODEL_UNAVAILABLE", "Configured AI model is not available.", False)


class KnowledgeAI:
    def __init__(self, settings):
        self.provider = "gemini"
        self.model_id = settings.gemini_model
        self.client = genai.Client(
            api_key=settings.gemini_api_key.get_secret_value(),
            http_options=types.HttpOptions(
                timeout=settings.studio_ai_timeout_seconds * 1000,
                retry_options=types.HttpRetryOptions(attempts=2, initial_delay=1.0),
            ),
        )

    def call(self, prompt: str, schema: type[BaseModel]) -> tuple[BaseModel, dict | None]:
        try:
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=schema.model_json_schema(),
                    thinking_config=types.ThinkingConfig(thinking_level="LOW"),
                    temperature=0,
                    max_output_tokens=4096,
                ),
            )
        except Exception as exc:
            raise _normalize(exc, _MODEL_MISSING) from None
        try:
            parsed = schema.model_validate_json(response.text or "")
        except (ValueError, TypeError):
            raise ProviderError(
                "AI_OUTPUT_INVALID", "AI returned invalid structured output.", False
            ) from None
        usage = (
            response.usage_metadata.model_dump(exclude_none=True, mode="json")
            if response.usage_metadata
            else None
        )
        return parsed, usage

    def close(self) -> None:
        self.client.close()
