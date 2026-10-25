"""Durable database-leased evaluator. Run with ``python -m app.worker``."""
import copy
import hashlib
import json
import logging
import signal
import time
from datetime import timedelta
from sqlalchemy import select, update, or_, and_
from .models import EvaluationJob, VivaSession, VivaReport, ProviderUsage, uid, now
from . import provider

log=logging.getLogger(__name__)


def process_once(database):
    """Claim one job with compare-and-swap, call provider outside transaction."""
    from . import service
    service.sweep_expired(database)
    instant=now()
    eligible=or_(and_(EvaluationJob.status=='queued',or_(EvaluationJob.next_attempt_at.is_(None),EvaluationJob.next_attempt_at<=instant)),
                 and_(EvaluationJob.status=='running',EvaluationJob.lease_until<instant))
    with database.session_factory.begin() as db:
        candidate=db.scalar(select(EvaluationJob).where(eligible,~select(ProviderUsage.id).where(ProviderUsage.session_id==EvaluationJob.session_id,ProviderUsage.status=='reserved',ProviderUsage.request_kind=='audio').exists()).order_by(EvaluationJob.created_at).limit(1))
        if not candidate:return False
        job_id=candidate.id
        # The claim token (lease timestamp) prevents a stale worker from publishing.
        lease=instant+timedelta(seconds=120)
        claimed=db.execute(update(EvaluationJob).where(EvaluationJob.id==job_id,eligible).values(status='running',lease_until=lease,attempt_count=EvaluationJob.attempt_count+1).execution_options(synchronize_session=False))
        if claimed.rowcount!=1:return False
        db.flush();db.expire_all()
        candidate=db.get(EvaluationJob,job_id)
        snapshot=copy.deepcopy(candidate.snapshot)
        snapshot_hash=candidate.snapshot_hash
        attempts=candidate.attempt_count
        sid=candidate.session_id
    try:
        if service.digest(snapshot)!=snapshot_hash:
            raise provider.InvalidAssessment('Frozen input hash mismatch')
        report=provider.evaluate(snapshot,database.settings,database=database)
        provider.validate_report(report,snapshot)
        with service._LOCK,database.session_factory.begin() as db:
            job=db.scalar(select(EvaluationJob).where(EvaluationJob.id==job_id).with_for_update())
            if job.status!='running' or service.aware(job.lease_until)!=lease:return True
            session=db.scalar(select(VivaSession).where(VivaSession.id==sid).with_for_update())
            existing=db.scalar(select(VivaReport).where(VivaReport.snapshot_hash==snapshot_hash))
            if existing:
                result=existing
            else:
                result=VivaReport(id=uid(),session_id=sid,snapshot_hash=snapshot_hash,version=1,created_at=now(),data={})
                report['id']=result.id
                report['snapshot_hash']=snapshot_hash
                result.data=report
                db.add(result)
            job.status='completed';job.result_id=result.id;job.error_code=None;job.retryable=False;job.lease_until=None
            session.result_id=result.id;session.status='completed';session.revision+=1
            service.event(db,session,'evaluation_updated',{'evaluation':service.job_dict(job)})
            service.state_event(db,session,database.settings)
    except service.DomainError as exc:
        _failure(database,job_id,lease,attempts,exc.code,terminal=exc.status in (429,503))
    except (provider.ProviderUnavailable,provider.InvalidAssessment) as exc:
        _failure(database,job_id,lease,attempts,'provider_unavailable' if isinstance(exc,provider.ProviderUnavailable) else 'invalid_assessment')
    except Exception:
        # Never log frozen answer/source payloads or provider response contents.
        log.error('Evaluation failed for job %s',job_id)
        _failure(database,job_id,lease,attempts,'evaluation_error')
    return True


def _failure(database,job_id,lease,attempts,code,terminal=False):
    from . import service
    with service._LOCK,database.session_factory.begin() as db:
        job=db.scalar(select(EvaluationJob).where(EvaluationJob.id==job_id).with_for_update())
        if not job or job.status!='running' or service.aware(job.lease_until)!=lease:return
        exhausted=terminal or attempts>=min(3,database.settings.evaluation_retry_limit)
        job.status='failed' if exhausted else 'queued'
        job.error_code=code;job.retryable=not exhausted;job.lease_until=None
        job.next_attempt_at=None if exhausted else now()+timedelta(seconds=1 if attempts==1 else 3)
        session=db.get(VivaSession,job.session_id)
        if exhausted:session.status='failed';session.revision+=1
        service.event(db,session,'evaluation_updated',{'evaluation':service.job_dict(job)})
        service.state_event(db,session,database.settings)


def main():
    from .db import get_database
    database=get_database();database.initialize()
    running=True
    def stop(*_):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    logging.basicConfig(level=logging.INFO)
    while running:
        if not process_once(database):time.sleep(.5)

if __name__=='__main__':main()
