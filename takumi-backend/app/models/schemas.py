from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

KUField = Literal["action", "cue", "interpretation", "decision", "exception"]
ReviewStatus = Literal["draft", "reviewed", "approved"]
EvidenceType = Literal["video_segment", "expert_response", "reviewer_correction"]
SourceLabel = Literal["real", "illustrative"]
Disposition = Literal["answered", "premise_rejected", "unobservable", "deferred"]
ClarificationStatus = Literal["pending", "answered", "premise_rejected", "unobservable", "deferred"]
GapType = Literal[
    "missing_reasoning",
    "changed_condition",
    "beginner_mistake",
    "missing_cue",
    "missing_exception",
    "unobservable_information",
]
QAStatus = Literal["answered", "needs_context", "insufficient_evidence"]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeUnit(BaseModel):
    id: str
    action: str
    cue: str | None = None
    interpretation: str | None = None
    decision: str | None = None
    exception: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    field_evidence: dict[KUField, list[str]] = Field(default_factory=dict)
    review_status: ReviewStatus = "draft"


class EvidenceItemOut(BaseModel):
    id: str
    task_id: str
    type: EvidenceType
    author: str
    author_id: str
    content: str | None = None
    media_id: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    source_label: SourceLabel
    created_at: datetime


class MediaOut(BaseModel):
    media_id: str
    task_id: str
    filename: str
    status: str
    duration_ms: int
    mime_type: str
    size_bytes: int
    sha256: str
    video_codec: str
    audio_codec: str | None
    has_audio: bool
    width: int
    height: int
    created_at: datetime
    playback_url: str
    playback_expires_at: datetime


class ProcessRequest(RequestModel):
    media_id: str = Field(min_length=1, max_length=100)
    mode: Literal["live", "demo"]
    request_id: str = Field(min_length=1, max_length=100)


class JobError(BaseModel):
    code: str
    message: str


PROGRESS_LABELS = {
    "uploaded": "Queued",
    "preparing_media": "Preparing recording for AI analysis",
    "extracting": "Extracting technical steps",
    "detecting_gaps": "Detecting knowledge gaps",
    "ready_for_review": "Draft extracted",
}


class JobOut(BaseModel):
    job_id: str
    task_id: str
    media_id: str
    mode: Literal["live", "demo"]
    status: Literal["queued", "running", "completed", "failed"]
    stage: Literal[
        "uploaded", "preparing_media", "extracting", "detecting_gaps", "ready_for_review"
    ]
    progress_label: str
    attempt_count: int
    draft_id: str | None
    error: JobError | None
    warnings: list[str]
    provider: str
    model_id: str
    prompt_version: str
    created_at: datetime
    updated_at: datetime


class ClarificationOut(BaseModel):
    id: str
    knowledge_unit_id: str
    question: str
    status: ClarificationStatus
    gap_type: GapType
    priority: int
    answer: str | None = None
    evidence_id: str | None = None
    answered_at: datetime | None = None


class ClarificationGenerate(RequestModel):
    draft_id: str
    expected_revision: int = Field(ge=1)
    mode: Literal["live", "deterministic"] = "live"
    request_id: str = Field(min_length=1, max_length=100)


class GeneratedClarificationOut(ClarificationOut):
    evidence_ids: list[str]
    missing_information: str
    rationale: str


class ClarificationGenerateOut(BaseModel):
    generation_id: str
    draft_id: str
    revision: int
    generation_mode: Literal["live", "deterministic", "fallback"]
    status: Literal["completed", "stale"]
    provider: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    warnings: list[str]
    questions: list[GeneratedClarificationOut]


class TaskCreate(RequestModel):
    title: str = Field(min_length=1, max_length=200)
    equipment: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    language: str = Field(default="en", pattern=r"^[a-z]{2}$")


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    equipment: str
    description: str | None
    language: str
    status: str
    active_release_id: str | None
    source_label: SourceLabel
    created_at: datetime
    updated_at: datetime


class TaskListOut(BaseModel):
    items: list[TaskOut]


class DraftOut(BaseModel):
    draft_id: str
    task_id: str
    revision: int
    status: str
    knowledge_units: list[KnowledgeUnit]
    clarifications: list[ClarificationOut]


class KnowledgeFieldsPatch(RequestModel):
    action: str | None = Field(default=None, min_length=1)
    cue: str | None = None
    interpretation: str | None = None
    decision: str | None = None
    exception: str | None = None

    @model_validator(mode="after")
    def validate_fields(self):
        if not self.model_fields_set:
            raise ValueError("At least one content field is required.")
        for field in self.model_fields_set:
            value = getattr(self, field)
            if field == "action" and value is None:
                raise ValueError("action cannot be null.")
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    raise ValueError("Content fields cannot be blank.")
                setattr(self, field, value)
        return self


class IntervalCorrection(RequestModel):
    evidence_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class DraftChange(RequestModel):
    knowledge_unit_id: str
    operation: Literal["update", "approve", "reject"] = "update"
    fields: KnowledgeFieldsPatch | None = None
    interval: IntervalCorrection | None = None

    @model_validator(mode="after")
    def validate_operation(self):
        if self.operation == "update":
            if self.fields is None and self.interval is None:
                raise ValueError("An update requires fields or an interval.")
        elif self.fields is not None or self.interval is not None:
            raise ValueError("Approval and rejection cannot include fields or an interval.")
        return self


class DraftPatch(RequestModel):
    expected_revision: int = Field(ge=1)
    changes: list[DraftChange] = Field(min_length=1, max_length=50)


class DraftPatchOut(BaseModel):
    draft_id: str
    revision: int
    status: str
    updated_knowledge_unit_ids: list[str]
    correction_evidence_ids: list[str]


class KnowledgeUpdateFields(RequestModel):
    action: str | None = None
    cue: str | None = None
    interpretation: str | None = None
    decision: str | None = None
    exception: str | None = None

    @model_validator(mode="after")
    def validate_non_null(self):
        for field in self.model_fields_set:
            value = getattr(self, field)
            if value is None or not value.strip():
                raise ValueError("Knowledge updates require non-blank, non-null content.")
            setattr(self, field, value.strip())
        return self


class KnowledgeUpdate(RequestModel):
    knowledge_unit_id: str
    fields: KnowledgeUpdateFields


class ClarificationRespond(RequestModel):
    expected_draft_revision: int = Field(ge=1)
    disposition: Disposition
    answer: str | None = Field(default=None, max_length=5000)
    knowledge_update: KnowledgeUpdate | None = None


class ClarificationRespondOut(BaseModel):
    clarification_id: str
    status: ClarificationStatus
    evidence_id: str | None
    knowledge_unit_id: str
    draft_revision: int


class PublishRequest(RequestModel):
    expected_revision: int = Field(ge=1)
    approval_note: str = Field(min_length=1, max_length=1000)


class PublishOut(BaseModel):
    release_id: str
    task_id: str
    version: int
    status: Literal["active", "superseded"]
    approved_by: str
    content_hash: str


class ReleaseOut(BaseModel):
    id: str
    task_id: str
    version: int
    status: Literal["active", "superseded"]
    approved_by: str
    approved_at: datetime
    approval_note: str
    content_hash: str
    knowledge_units: list[KnowledgeUnit]
    evidence_items: list[EvidenceItemOut]


class TrainingPackKU(BaseModel):
    id: str
    action: str
    cue: str | None = None
    interpretation: str | None = None
    decision: str | None = None
    exception: str | None = None
    evidence_ids: list[str]
    field_evidence: dict[KUField, list[str]]


class TrainingPackEvidence(BaseModel):
    id: str
    type: EvidenceType
    author: str
    content: str | None
    source_label: SourceLabel
    media_id: str | None
    start_ms: int | None
    end_ms: int | None


class TrainingPack(BaseModel):
    schema_version: Literal["1.0"]
    task_id: str
    release_id: str
    version: int
    title: str
    equipment: str
    language: str
    source_label: SourceLabel
    approved_by: str
    approved_at: datetime
    content_hash: str
    knowledge_units: list[TrainingPackKU]
    evidence_items: list[TrainingPackEvidence]


class AskRequest(RequestModel):
    question: str = Field(min_length=1, max_length=1000)
    language: str = "en"


class Claim(BaseModel):
    text: str
    knowledge_unit_id: str
    field: KUField
    evidence_ids: list[str]


class AskOut(BaseModel):
    status: QAStatus
    release_id: str
    claims: list[Claim]
    missing_information: list[str]
    review_required: bool


class DemoLoginRequest(RequestModel):
    demo_user: Literal["expert", "reviewer", "apprentice", "admin"]


class DemoUserOut(BaseModel):
    id: str
    role: Literal["expert", "reviewer", "apprentice", "admin"]
    display_name: str


class DemoLoginOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"]
    expires_at: datetime
    user: DemoUserOut


class DemoResetRequest(RequestModel):
    scope: Literal["attempts", "full_demo"]
    confirm: str | None = None
    state: Literal["fresh", "approved_baseline"] = "fresh"


class DemoResetOut(BaseModel):
    success: bool
    scope: Literal["attempts", "full_demo"]
    state: Literal["fresh", "approved_baseline"]


class HealthOut(BaseModel):
    status: str
    database: str
    app_env: str
    demo_mode: bool
    ai_provider: str
    schema_version: int
    features: dict[str, bool]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict


class ErrorEnvelope(BaseModel):
    error: ErrorBody
