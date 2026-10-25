import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Takumi Knowledge Engine"
    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./data/takumi.db"
    demo_mode: bool = True
    enable_demo_auth: bool = True
    enable_demo_seed: bool = True
    enable_demo_reset: bool = True
    demo_seed_state: Literal["fresh", "approved_baseline"] = "approved_baseline"
    enable_grounded_qa: bool = True
    enable_training_pack_export: bool = True
    demo_signing_secret: SecretStr | None = None
    demo_token_ttl_minutes: int = Field(480, ge=1, le=1440)
    ai_provider: Literal["fixture", "gemini", "ollama", "openrouter"] = "fixture"
    enable_live_extraction: bool = False
    enable_live_gap_generation: bool = False
    enable_live_qa: bool = False
    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = None
    ollama_base_url: str | None = None
    ollama_model: str | None = None
    whisper_base_url: str | None = None
    openrouter_api_key: SecretStr | None = None
    openrouter_model: str | None = None
    enable_media_upload: bool = False
    enable_media_processing: bool = False
    enable_media_streaming: bool = False
    media_storage_dir: Path = Path("./data/media")
    max_upload_mb: int = Field(100, ge=1)
    max_video_duration_seconds: int = Field(240, ge=1)
    enable_processing_worker: bool = False
    processing_max_retries: int = Field(2, ge=0, le=5)
    processing_max_concurrent_jobs: int = Field(1, ge=1)
    processing_poll_interval_seconds: float = Field(2.0, gt=0)
    processing_job_lease_seconds: int = Field(180, ge=30)
    processing_provider_timeout_seconds: int = Field(120, ge=10)
    enable_multilingual: bool = False
    enable_embeddings: bool = False
    enable_advanced_retrieval: bool = False
    enable_qa_support_validation: bool = True
    enable_ai_eval_logging: bool = True
    enable_deterministic_gap_fallback: bool = True
    enable_qa_abstention: bool = True
    enable_ai_viva_scoring: bool = False
    log_level: str = "INFO"
    log_model_inputs: bool = False
    cors_origins: str = "http://localhost:3000"
    max_request_body_kb: int = 1024
    enable_studio_ai_analysis: bool = True
    studio_ai_model: str | None = None
    studio_ai_timeout_seconds: int = Field(45, ge=5, le=120)
    studio_ai_thinking_level: Literal["low", "medium", "high"] = "low"
    course_video_url: str = ""
    course_video_duration_seconds: int = Field(60, ge=1, le=86400)

    @property
    def studio_model(self) -> str:
        return self.studio_ai_model or self.gemini_model or "gemini-3.5-flash-lite"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def validate_capabilities(self):
        for field in ("gemini_model", "studio_ai_model"):
            value = getattr(self, field)
            if value and value.strip() and not re.fullmatch(r"gemini-3[\w.-]*", value):
                raise ValueError(
                    f"{field.upper()} must be a Gemini 3-series model id (gemini-3...)"
                )
        reserved = {
            "log_model_inputs": 6,
            "enable_multilingual": 6,
            "enable_embeddings": 6,
            "enable_advanced_retrieval": 6,
            "enable_ai_viva_scoring": 6,
        }
        for field, checkpoint in reserved.items():
            if getattr(self, field):
                name = field.upper()
                raise ValueError(
                    f"{name} is reserved for Checkpoint {checkpoint} and not implemented yet"
                )
        if self.ai_provider == "gemini" and (
            not self.gemini_api_key
            or not self.gemini_api_key.get_secret_value().strip()
            or not self.gemini_model
            or not self.gemini_model.strip()
        ):
            raise ValueError("AI_PROVIDER=gemini requires GEMINI_API_KEY and GEMINI_MODEL")
        if self.ai_provider == "ollama" and not all(
            (value or "").strip()
            for value in (self.ollama_base_url, self.ollama_model, self.whisper_base_url)
        ):
            raise ValueError(
                "AI_PROVIDER=ollama requires OLLAMA_BASE_URL, OLLAMA_MODEL and WHISPER_BASE_URL"
            )
        if self.ai_provider == "openrouter" and (
            not self.openrouter_api_key
            or not self.openrouter_api_key.get_secret_value().strip()
            or not (self.openrouter_model or "").strip()
            or not (self.whisper_base_url or "").strip()
        ):
            raise ValueError(
                "AI_PROVIDER=openrouter requires OPENROUTER_API_KEY, OPENROUTER_MODEL "
                "and WHISPER_BASE_URL"
            )
        for field in ("enable_live_gap_generation", "enable_live_qa"):
            if getattr(self, field) and self.ai_provider != "gemini":
                raise ValueError(f"{field.upper()} requires AI_PROVIDER=gemini")
        if self.enable_live_qa and not (
            self.enable_qa_support_validation and self.enable_qa_abstention
        ):
            raise ValueError(
                "ENABLE_LIVE_QA requires ENABLE_QA_SUPPORT_VALIDATION and ENABLE_QA_ABSTENTION"
            )
        if self.enable_live_extraction and not (
            self.ai_provider in ("gemini", "ollama", "openrouter") and self.enable_media_processing
        ):
            raise ValueError(
                "ENABLE_LIVE_EXTRACTION requires AI_PROVIDER=gemini, ollama or openrouter "
                "and ENABLE_MEDIA_PROCESSING=true"
            )
        if self.enable_media_processing and not self.enable_media_upload:
            raise ValueError("ENABLE_MEDIA_PROCESSING requires ENABLE_MEDIA_UPLOAD=true")
        if self.enable_processing_worker and not self.enable_media_processing:
            raise ValueError("ENABLE_PROCESSING_WORKER requires ENABLE_MEDIA_PROCESSING=true")
        if self.processing_job_lease_seconds <= self.processing_provider_timeout_seconds:
            raise ValueError(
                "PROCESSING_JOB_LEASE_SECONDS must exceed PROCESSING_PROVIDER_TIMEOUT_SECONDS"
            )
        for field in ("demo_mode", "enable_demo_auth", "enable_demo_reset", "enable_demo_seed"):
            if self.app_env == "production" and getattr(self, field):
                raise ValueError(f"{field.upper()} must be false when APP_ENV=production")
        for field in ("enable_demo_auth", "enable_demo_reset", "enable_demo_seed"):
            if not self.demo_mode and getattr(self, field):
                raise ValueError(f"{field.upper()} requires DEMO_MODE=true")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
