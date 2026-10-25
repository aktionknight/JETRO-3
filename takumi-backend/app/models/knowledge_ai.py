from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, utcnow


class KnowledgeAIRun(Base):
    """Provenance ledger for gap elicitation (replay receipts) and grounded Q&A call logs."""

    __tablename__ = "knowledge_ai_runs"
    __table_args__ = (UniqueConstraint("task_id", "operation", "request_id"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    operation: Mapped[str]  # gap_elicitation | grounded_qa
    request_id: Mapped[str | None]
    draft_id: Mapped[str | None]
    draft_revision: Mapped[int | None]
    release_id: Mapped[str | None]
    snapshot_hash: Mapped[str]
    generation_mode: Mapped[str]  # live | deterministic | fallback
    provider: Mapped[str]
    model_id: Mapped[str | None]
    prompt_version: Mapped[str | None]
    latency_ms: Mapped[int | None]
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    requested_by: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
