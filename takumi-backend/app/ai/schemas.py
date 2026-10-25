from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.schemas import Claim, EvidenceType, GapType, KnowledgeUnit, QAStatus, SourceLabel


class TaskContext(BaseModel):
    task_id: str
    title: str
    equipment: str
    language: str


class MediaReference(BaseModel):
    fixture_id: str | None = None
    media_id: str | None = None


class Provenance(BaseModel):
    provider: str
    model_id: str | None = None
    prompt_version: str | None = None
    fixture_id: str | None = None


class ExtractedEvidence(BaseModel):
    id: str
    type: EvidenceType
    author_id: str
    author_label: str
    content: str | None = None
    media_id: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    source_label: SourceLabel


class ExtractionResult(BaseModel):
    knowledge_units: list[KnowledgeUnit]
    evidence_items: list[ExtractedEvidence]
    provenance: Provenance


class ClarificationCandidate(BaseModel):
    knowledge_unit_id: str
    gap_type: GapType
    priority: int
    question: str


class ApprovedContext(BaseModel):
    release_id: str
    knowledge_units: list[KnowledgeUnit]
    evidence_ids: set[str]
    evidence_items: list[dict] = Field(default_factory=list)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GapCandidate(_Strict):
    knowledge_unit_id: str
    gap_type: GapType
    missing_information: str = Field(min_length=1, max_length=1000)
    rationale: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(max_length=10)
    suggested_question: str = Field(min_length=1, max_length=500)
    importance: int = Field(ge=1, le=5)
    requires_expert: bool = True


class GapAnalysis(_Strict):
    candidates: list[GapCandidate] = Field(max_length=12)
    warnings: list[str] = Field(default_factory=list, max_length=10)


class GroundedClaim(Claim):
    model_config = ConfigDict(extra="forbid")


class GroundedAnswer(_Strict):
    status: QAStatus
    claims: list[GroundedClaim] = Field(default_factory=list, max_length=5)
    missing_information: list[str] = Field(default_factory=list, max_length=5)
    review_required: bool = False


class QAProviderResult(BaseModel):
    status: QAStatus
    claims: list[Claim] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    provenance: Provenance


class ExtractedStep(BaseModel):
    start_ms: int
    end_ms: int
    action: str
    cue: str | None = None
    interpretation: str | None = None
    decision: str | None = None
    exception: str | None = None
    source_modality: Literal["visual", "spoken", "visual_and_spoken"]
    evidence_description: str | None = None


class VideoExtractionResult(BaseModel):
    task_summary: str
    detected_language: str | None = None
    steps: list[ExtractedStep] = Field(default_factory=list)
    processing_warnings: list[str] = Field(default_factory=list)


class GeminiStep(BaseModel):
    """Gemini video step; the provider converts MM:SS text to ExtractedStep milliseconds."""

    start: str
    end: str
    action: str
    cue: str | None = None
    interpretation: str | None = None
    decision: str | None = None
    exception: str | None = None
    source_modality: Literal["visual", "spoken", "visual_and_spoken"]
    evidence_description: str | None = None


class GeminiVideoResult(BaseModel):
    task_summary: str
    detected_language: str | None = None
    steps: list[GeminiStep] = Field(default_factory=list)
    processing_warnings: list[str] = Field(default_factory=list)


class ProviderFile(BaseModel):
    name: str
    uri: str
    mime_type: str
    state: str  # PROCESSING | ACTIVE | FAILED
    expires_at: datetime | None = None


class VideoExtraction(BaseModel):
    result: VideoExtractionResult
    usage: dict[str, Any] | None = None
    model_id: str
    prompt_version: str
