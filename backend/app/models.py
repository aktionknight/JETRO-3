"""Portable relational model; immutable release/snapshot payloads use JSON."""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import Column, String, Integer, Float, Boolean, JSON, ForeignKey, UniqueConstraint, DateTime
from sqlalchemy.orm import declarative_base

Base = declarative_base()
def uid(): return uuid4().hex
def now(): return datetime.now(timezone.utc)

class Release(Base):
    __tablename__='releases'
    id=Column(String,primary_key=True)
    task_id=Column(String,nullable=False)
    title=Column(String,nullable=False)
    content_hash=Column(String,nullable=False)
    data=Column(JSON,nullable=False)

class PlaygroundAttempt(Base):
    __tablename__='playground_attempts'
    id=Column(String,primary_key=True)
    learner_id=Column(String,index=True,nullable=False)
    task_id=Column(String,nullable=False)
    status=Column(String,nullable=False)
    release_id=Column(String,ForeignKey('releases.id'),nullable=False)
    bundle_id=Column(String,nullable=False)
    bundle_hash=Column(String,nullable=False)
    scenario_id=Column(String,nullable=False)
    data=Column(JSON,nullable=False)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)

class VivaSession(Base):
    __tablename__='viva_sessions'
    id=Column(String,primary_key=True,default=uid)
    learner_id=Column(String,index=True,nullable=False)
    attempt_id=Column(String,ForeignKey('playground_attempts.id'),unique=True,nullable=False)
    release_id=Column(String,ForeignKey('releases.id'),nullable=False)
    bundle_id=Column(String,nullable=False)
    bundle_hash=Column(String,nullable=False)
    status=Column(String,nullable=False,default='created')
    input_mode=Column(String,nullable=False)
    revision=Column(Integer,nullable=False,default=0)
    active_seconds=Column(Float,nullable=False,default=0)
    active_since=Column(DateTime(timezone=True),nullable=True)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)
    expires_at=Column(DateTime(timezone=True),nullable=False)
    current_index=Column(Integer,nullable=False,default=0)
    current_question=Column(JSON,nullable=True)
    coverage=Column(JSON,nullable=False,default=list)
    connection_epoch=Column(Integer,nullable=False,default=0)
    event_sequence=Column(Integer,nullable=False,default=0)
    rubric_version=Column(String,nullable=False,default='rubric-v1')
    prompt_version=Column(String,nullable=False,default='examiner-v1')
    model_config_version=Column(String,nullable=False,default='fixture-v1')
    evaluation_job_id=Column(String,nullable=True)
    result_id=Column(String,nullable=True)
    finish_reason=Column(String,nullable=True)
    pending_replacement_id=Column(String,nullable=True)

class InterviewTurn(Base):
    __tablename__='viva_turns'
    id=Column(String,primary_key=True,default=uid)
    session_id=Column(String,ForeignKey('viva_sessions.id',ondelete='CASCADE'),nullable=False,index=True)
    sequence=Column(Integer,nullable=False)
    question_id=Column(String,nullable=True)
    speaker=Column(String,nullable=False)
    text=Column(String,nullable=False)
    input_mode=Column(String,nullable=False)
    delivery=Column(String,nullable=False,default='completed')
    client_turn_id=Column(String,nullable=True)
    replacement_id=Column(String,nullable=True)
    quality_flag=Column(String,nullable=True)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)
    __table_args__=(UniqueConstraint('session_id','sequence'),UniqueConstraint('session_id','client_turn_id'))

class SessionEvent(Base):
    __tablename__='viva_events'
    id=Column(String,primary_key=True,default=uid)
    session_id=Column(String,ForeignKey('viva_sessions.id',ondelete='CASCADE'),nullable=False,index=True)
    sequence=Column(Integer,nullable=False)
    session_revision=Column(Integer,nullable=False)
    type=Column(String,nullable=False)
    payload=Column(JSON,nullable=False)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)
    __table_args__=(UniqueConstraint('session_id','sequence'),)

class ProviderReceipt(Base):
    __tablename__='viva_provider_receipts'
    id=Column(String,primary_key=True,default=uid)
    session_id=Column(String,ForeignKey('viva_sessions.id',ondelete='CASCADE'),nullable=False)
    epoch=Column(Integer,nullable=False)
    event_id=Column(String,nullable=False)
    semantic_key=Column(String,nullable=False)
    __table_args__=(UniqueConstraint('session_id','epoch','event_id'),UniqueConstraint('session_id','epoch','semantic_key'))

class VoiceConnection(Base):
    __tablename__='viva_connections'
    id=Column(String,primary_key=True,default=uid)
    session_id=Column(String,ForeignKey('viva_sessions.id',ondelete='CASCADE'),nullable=False,index=True)
    epoch=Column(Integer,nullable=False)
    provider_call_id=Column(String,nullable=True)
    pending_utterance_id=Column(String,nullable=True)
    pending_question_id=Column(String,nullable=True)
    pending_since=Column(DateTime(timezone=True),nullable=True)
    drain_started_at=Column(DateTime(timezone=True),nullable=True)
    status=Column(String,nullable=False,default='connecting')
    lease_until=Column(DateTime(timezone=True),nullable=True)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)

class EvaluationJob(Base):
    __tablename__='viva_evaluation_jobs'
    id=Column(String,primary_key=True,default=uid)
    session_id=Column(String,ForeignKey('viva_sessions.id',ondelete='CASCADE'),unique=True,nullable=False)
    snapshot=Column(JSON,nullable=False)
    snapshot_hash=Column(String,nullable=False)
    status=Column(String,nullable=False,default='queued')
    attempt_count=Column(Integer,nullable=False,default=0)
    result_id=Column(String,nullable=True)
    error_code=Column(String,nullable=True)
    retryable=Column(Boolean,nullable=False,default=True)
    lease_until=Column(DateTime(timezone=True),nullable=True)
    next_attempt_at=Column(DateTime(timezone=True),nullable=True)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)

class VivaReport(Base):
    __tablename__='viva_reports'
    id=Column(String,primary_key=True,default=uid)
    session_id=Column(String,ForeignKey('viva_sessions.id',ondelete='CASCADE'),nullable=False,index=True)
    snapshot_hash=Column(String,unique=True,nullable=False)
    version=Column(Integer,nullable=False,default=1)
    data=Column(JSON,nullable=False)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)

class IdempotencyRecord(Base):
    __tablename__='idempotency_records'
    id=Column(String,primary_key=True,default=uid)
    actor_id=Column(String,nullable=False)
    route=Column(String,nullable=False)
    key=Column(String,nullable=False)
    request_hash=Column(String,nullable=False)
    response=Column(JSON,nullable=False)
    session_id=Column(String,nullable=True)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)
    __table_args__=(UniqueConstraint('actor_id','route','key'),)

class AuditEvent(Base):
    __tablename__='audit_events'
    id=Column(String,primary_key=True,default=uid)
    actor_id=Column(String,nullable=False)
    action=Column(String,nullable=False)
    target_id=Column(String,nullable=False)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)

class ProviderUsage(Base):
    __tablename__='viva_provider_usage'
    id=Column(String,primary_key=True,default=uid)
    session_id=Column(String,ForeignKey('viva_sessions.id',ondelete='CASCADE'),nullable=False,index=True)
    request_kind=Column(String,nullable=False)
    price_version=Column(String,nullable=False)
    reserved_microdollars=Column(Integer,nullable=False)
    actual_microdollars=Column(Integer,nullable=True)
    status=Column(String,nullable=False,default='reserved')
    usage=Column(JSON,nullable=False,default=dict)
    created_at=Column(DateTime(timezone=True),default=now,nullable=False)
    settled_at=Column(DateTime(timezone=True),nullable=True)
