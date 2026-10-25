"""Transactional interview rules shared by text and server-observed voice input."""
import copy
import hashlib
import json
import threading
from functools import wraps
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, text
from .models import (VivaSession, PlaygroundAttempt, Release, InterviewTurn, SessionEvent,
    EvaluationJob, VivaReport, IdempotencyRecord, VoiceConnection, ProviderReceipt, uid, now)

_LOCK=threading.RLock()
_SESSION_LOCKS={}

def serialized_provider_call(function):
    @wraps(function)
    def serialized(database,sid,*args,**kwargs):
        with _LOCK:
            lock=_SESSION_LOCKS.setdefault(sid,threading.RLock())
        with lock:
            return function(database,sid,*args,**kwargs)
    return serialized

class DomainError(Exception):
    def __init__(self,status,code,message,details=None):
        self.status=status;self.code=code;self.message=message;self.details=details or {}

def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

def stamp(value): return aware(value).isoformat() if value else None

def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def owned(db,sid,actor,lock=False):
    q=select(VivaSession).where(VivaSession.id==sid,VivaSession.learner_id==actor)
    if lock:q=q.with_for_update()
    session=db.scalar(q)
    if not session:raise DomainError(404,'not_found','Interview not found')
    return session

def revision(session,expected):
    if session.revision != expected:
        raise DomainError(409,'stale_revision','Refresh interview state before retrying',{'revision':session.revision})

def turn_dict(turn):
    return {'id':turn.id,'sequence':turn.sequence,'question_id':turn.question_id,'speaker':turn.speaker,
        'text':turn.text,'input_mode':turn.input_mode,'delivery':turn.delivery,'replacement_id':turn.replacement_id,
        'quality_flag':turn.quality_flag,'created_at':stamp(turn.created_at)}

def turns_for(db,sid):
    return [turn_dict(t) for t in db.scalars(select(InterviewTurn).where(InterviewTurn.session_id==sid).order_by(InterviewTurn.sequence))]

def elapsed(session):
    return session.active_seconds + ((now()-aware(session.active_since)).total_seconds() if session.active_since else 0)

def pause_clock(session):
    session.active_seconds=elapsed(session);session.active_since=None

def public_session(db,session,settings=None):
    return {'id':session.id,'attempt_id':session.attempt_id,'learner_id':session.learner_id,
        'release_id':session.release_id,'bundle_id':session.bundle_id,'bundle_hash':session.bundle_hash,
        'rubric_version':session.rubric_version,'prompt_version':session.prompt_version,'model_config_version':session.model_config_version,
        'status':session.status,'input_mode':session.input_mode,'revision':session.revision,
        'question_count':3,'answered_count':sum(c.get('status')=='answered' for c in session.coverage),
        'follow_up_count':sum(c.get('follow_up_count',0) for c in session.coverage),
        'current_question':session.current_question,'last_event_sequence':session.event_sequence,
        'evaluation_job_id':session.evaluation_job_id,'result_id':session.result_id,
        'active_seconds':round(elapsed(session),2),'max_active_seconds':settings.max_active_seconds if settings else 600,
        'transcript':turns_for(db,session.id),'created_at':stamp(session.created_at),'expires_at':stamp(session.expires_at)}

def job_dict(job):
    return {'id':job.id,'session_id':job.session_id,'status':job.status,'attempt_count':job.attempt_count,
        'result_id':job.result_id,'error_code':job.error_code,'retryable':job.retryable}

def event(db,session,kind,payload):
    session.event_sequence+=1
    value=SessionEvent(id=uid(),session_id=session.id,sequence=session.event_sequence,session_revision=session.revision,type=kind,payload=payload,created_at=now())
    db.add(value)
    return value

def state_event(db,session,settings):
    # Session events exclude transcript duplication; durable turn events carry committed content.
    data=public_session(db,session,settings)
    data.pop('transcript',None)
    event(db,session,'session_updated',{'session':data})

def add_turn(db,session,speaker,content,question_id,mode,client_id=None,delivery='completed',replacement_id=None):
    sequence=(db.scalar(select(func.max(InterviewTurn.sequence)).where(InterviewTurn.session_id==session.id)) or 0)+1
    turn=InterviewTurn(id=uid(),session_id=session.id,sequence=sequence,question_id=question_id,speaker=speaker,text=content,input_mode=mode,client_turn_id=client_id,delivery=delivery,replacement_id=replacement_id,created_at=now())
    db.add(turn);db.flush()
    event(db,session,'turn_committed',{'turn':turn_dict(turn)})
    return turn

def deliver(db,session,question,settings):
    session.current_question=copy.deepcopy(question)
    add_turn(db,session,'interviewer',question['prompt'],question['id'],session.input_mode,delivery='completed' if session.input_mode=='text' else 'unconfirmed')
    event(db,session,'question_presented',{'question':question})

def validate_pack(release,attempt,settings):
    pack=release.data
    if not settings.demo_database and pack.get('content_status')!='approved':
        raise DomainError(422,'unapproved_content','Operational assessment requires instructor-approved source content')
    if pack.get('id')!=attempt.release_id or pack.get('task_id')!=attempt.task_id:
        raise DomainError(422,'release_mismatch','Attempt and knowledge release do not match')
    if len(pack.get('question_plan',[]))!=3 or not pack.get('rubric') or not pack.get('documents'):
        raise DomainError(422,'missing_reviewed_content','Question plan, rubric, and source manifest are required')
    if any(d.get('availability')!='available' for d in pack['documents']):
        raise DomainError(422,'missing_source','A required knowledge source is unavailable')
    # Conservative byte upper bound never silently truncates a large pack.
    if len(json.dumps(pack,ensure_ascii=False).encode('utf-8')) > settings.max_context_tokens*3:
        raise DomainError(422,'knowledge_pack_too_large','Knowledge pack exceeds the configured context budget')
    if attempt.data.get('release_id')!=attempt.release_id or attempt.data.get('bundle_hash')!=attempt.bundle_hash:
        raise DomainError(422,'release_mismatch','Attempt provenance does not match its bundle')

def mutate(database,actor,route,key,payload,callback,session_id=None):
    if not key or len(key)>128:raise DomainError(422,'idempotency_key_required','Provide an Idempotency-Key of 1–128 characters')
    request_hash=digest(payload)
    with _LOCK:
        with database.session_factory() as db:
            if database.engine.dialect.name=='sqlite':db.execute(text('BEGIN IMMEDIATE'))
            else:db.begin()
            try:
                record=db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.actor_id==actor,IdempotencyRecord.route==route,IdempotencyRecord.key==key))
                if record:
                    if record.request_hash!=request_hash:raise DomainError(409,'idempotency_conflict','The idempotency key was used with a different payload')
                    result=record.response;db.rollback();return result
                result=callback(db)
                db.add(IdempotencyRecord(actor_id=actor,route=route,key=key,request_hash=request_hash,response=result,session_id=session_id or (result.get('id') if route=='/viva/sessions' else None)))
                db.commit();return result
            except Exception:
                db.rollback();raise

def start(db,actor,payload,settings):
    attempt=db.get(PlaygroundAttempt,payload['attempt_id'])
    if not attempt or attempt.learner_id!=actor:raise DomainError(404,'not_found','Attempt not found')
    existing=db.scalar(select(VivaSession).where(VivaSession.attempt_id==attempt.id))
    if existing:return public_session(db,existing,settings)
    if attempt.status!='completed':raise DomainError(409,'attempt_incomplete','Complete the Playground attempt first')
    recent=db.scalar(select(func.count()).select_from(VivaSession).where(VivaSession.learner_id==actor,VivaSession.created_at>=now()-timedelta(hours=1)))
    if recent>=5:raise DomainError(429,'session_rate_limit','Session creation limit reached; try later')
    release=db.get(Release,attempt.release_id)
    if not release:raise DomainError(422,'missing_release','Approved release not found')
    if not settings.demo_database and release.data.get('tenant_id')!=getattr(actor,'tenant_id',None):
        raise DomainError(404,'not_found','Approved release not found')
    validate_pack(release,attempt,settings)
    instant=now()
    session=VivaSession(id=uid(),learner_id=actor,attempt_id=attempt.id,release_id=attempt.release_id,bundle_id=attempt.bundle_id,bundle_hash=attempt.bundle_hash,
        input_mode=payload['input_mode'],status='in_progress' if payload['input_mode']=='text' else 'created',revision=1,created_at=instant,expires_at=instant+timedelta(seconds=settings.session_expiry_seconds),active_seconds=0,
        active_since=instant if payload['input_mode']=='text' else None,current_index=0,event_sequence=0,connection_epoch=0,
        coverage=[{'question_id':q['id'],'objective':q['objective'],'status':'unanswered','answer_turn_ids':[],'follow_up_count':0} for q in release.data['question_plan']],
        rubric_version=release.data['rubric_version'],prompt_version='examiner-v1',model_config_version='fixture-v1' if settings.provider_mode=='fixture' else settings.text_model+'|'+settings.voice_model)
    db.add(session);db.flush()
    deliver(db,session,release.data['question_plan'][0],settings)
    state_event(db,session,settings)
    return public_session(db,session,settings)

def check_active(session,settings,allow_grace=False):
    if session.status!='in_progress':raise DomainError(409,'invalid_state','Interview is not accepting answers')
    if not allow_grace and now()>=aware(session.expires_at):raise DomainError(409,'session_expired','Interview expired; finish to receive an incomplete evaluation')
    if not allow_grace and elapsed(session)>=settings.max_active_seconds:raise DomainError(409,'time_limit','Active interview time limit reached; finish to receive your evaluation')

def snapshot(db,session,settings):
    release=copy.deepcopy(db.get(Release,session.release_id).data)
    attempt=db.get(PlaygroundAttempt,session.attempt_id)
    return {'session':public_session(db,session,settings),'turns':turns_for(db,session.id),'coverage':copy.deepcopy(session.coverage),
        'release':release,'rubric':release['rubric'],'playground':copy.deepcopy(attempt.data),'finish_reason':session.finish_reason,
        'provider_mode':settings.provider_mode,'settings':{'voice_model':settings.voice_model,'text_model':settings.text_model,'prompt_version':session.prompt_version}}

def _pending_connection(db,session):
    return db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==session.id,
        VoiceConnection.epoch==session.connection_epoch,
        VoiceConnection.status.in_(['active','connecting']),
        VoiceConnection.pending_utterance_id.is_not(None)))


def _pending_state(connection):
    if not connection or not connection.pending_since:
        return {'pending':False,'id':None,'epoch':None,'remaining_seconds':0.0}
    seconds=max(0.0,(now()-aware(connection.drain_started_at)).total_seconds()) if connection.drain_started_at else 0.0
    return {'pending':True,'id':connection.pending_utterance_id,'epoch':connection.epoch,
        'remaining_seconds':max(0.0,10.0-seconds)}


def pending_voice_input(database,sid,actor,begin_drain=False):
    with _LOCK,database.session_factory.begin() as db:
        session=owned(db,sid,actor,True)
        connection=_pending_connection(db,session)
        if connection and begin_drain and not connection.drain_started_at:
            connection.drain_started_at=now()
        return _pending_state(connection)


def voice_input_started(database,sid,actor,epoch,item_id):
    with _LOCK,database.session_factory.begin() as db:
        session=owned(db,sid,actor,True)
        if session.connection_epoch!=epoch or session.input_mode!='voice':
            raise DomainError(409,'connection_expired','Voice connection is no longer active')
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid,VoiceConnection.epoch==epoch))
        if not connection or connection.status!='active':raise DomainError(409,'connection_expired','Voice connection is no longer active')
        # Replayed VAD must never reset the ten-second deadline.
        if connection.pending_utterance_id==item_id:return _pending_state(connection)
        receipt=db.scalar(select(ProviderReceipt).where(ProviderReceipt.session_id==sid,ProviderReceipt.epoch==epoch,ProviderReceipt.semantic_key==item_id))
        if receipt:return _pending_state(connection)
        if connection.drain_started_at:raise DomainError(409,'utterance_draining','The interview is finishing the accepted utterance')
        check_active(session,database.settings)
        if not session.current_question:raise DomainError(409,'question_conflict','Answer the current question')
        db.add(ProviderReceipt(session_id=sid,epoch=epoch,event_id=item_id,semantic_key=item_id))
        if connection.pending_utterance_id:return _pending_state(connection)
        connection.pending_utterance_id=item_id;connection.pending_question_id=session.current_question['id'];connection.pending_since=now()
        return _pending_state(connection)


def voice_input_finished(database,sid,actor,epoch,item_id):
    with _LOCK,database.session_factory.begin() as db:
        session=owned(db,sid,actor,True)
        if session.connection_epoch!=epoch:return False
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid,VoiceConnection.epoch==epoch))
        if not connection or connection.pending_utterance_id!=item_id:return False
        connection.pending_utterance_id=None;connection.pending_question_id=None;connection.pending_since=None
        return True


def _pending_interrupt(db,session,connection):
    if connection and connection.pending_utterance_id:
        question_id=connection.pending_question_id
        add_turn(db,session,'learner','[Speech did not finalize before the interview stopped.]',question_id,'voice',
            client_id=f'voice-interrupted:{connection.epoch}:{connection.pending_utterance_id}',delivery='interrupted')
        connection.pending_utterance_id=None;connection.pending_question_id=None;connection.pending_since=None


def finish(db,session,settings,reason='learner_finished',increment_revision=True):
    if session.evaluation_job_id:return job_dict(db.get(EvaluationJob,session.evaluation_job_id))
    if session.status not in ('created','connecting','in_progress','paused'):raise DomainError(409,'invalid_state','Interview cannot be finished in this state')
    pending=_pending_connection(db,session)
    if pending and not pending.drain_started_at:pending.drain_started_at=now()
    drain=_pending_state(pending)
    if drain['pending'] and drain['remaining_seconds']>0:
        raise DomainError(409,'utterance_in_progress','Allow the accepted utterance to finish before evaluation',{'retry_after':drain['remaining_seconds']})
    pause_clock(session);session.status='evaluating';session.finish_reason=reason
    if increment_revision:session.revision+=1
    _pending_interrupt(db,session,pending)
    session.connection_epoch+=1
    for conn in db.scalars(select(VoiceConnection).where(VoiceConnection.session_id==session.id,VoiceConnection.status.in_(['active','connecting']))):conn.status='closed'
    frozen=snapshot(db,session,settings)
    job=EvaluationJob(id=uid(),session_id=session.id,snapshot=frozen,snapshot_hash=digest(frozen),status='queued',attempt_count=0,retryable=True,created_at=now())
    session.evaluation_job_id=job.id;db.add(job);db.flush()
    event(db,session,'evaluation_updated',{'evaluation':job_dict(job)});state_event(db,session,settings)
    return job_dict(job)

def answer_context(database,sid,actor,payload):
    with database.session_factory() as db:
        session=owned(db,sid,actor)
        return {'session':public_session(db,session,database.settings),'turns':turns_for(db,sid),
            'release':copy.deepcopy(db.get(Release,session.release_id).data),'question':session.current_question,
            'answer':payload['text'],'provider_mode':database.settings.provider_mode,
            'follow_up_count':session.coverage[session.current_index].get('follow_up_count',0) if session.current_index<3 else 0,
            'current_index':session.current_index,'_settings':database.settings,'_database':database}

def accept_answer(db,session,payload,settings,plan,mode='text'):
    # Semantic deduplication is checked before revision to support retries with new keys.
    previous=db.scalar(select(InterviewTurn).where(InterviewTurn.session_id==session.id,InterviewTurn.client_turn_id==payload['client_turn_id']))
    if previous:
        if previous.text!=payload['text'] or previous.question_id!=payload['question_id']:
            raise DomainError(409,'turn_conflict','This turn ID already contains another answer')
        return {'session':public_session(db,session,settings),'turn':turn_dict(previous)}
    pending=_pending_connection(db,session) if mode=='voice' else None
    if pending and (payload['client_turn_id']!=f'voice:{pending.epoch}:{pending.pending_utterance_id}' or
            not pending.pending_question_id or pending.pending_question_id!=payload['question_id']):
        pending=None
    drain=_pending_state(pending)
    allow_grace=drain['pending'] and (not pending.drain_started_at or drain['remaining_seconds']>0)
    revision(session,payload['expected_revision']);check_active(session,settings,allow_grace=allow_grace)
    if session.input_mode!=mode:raise DomainError(409,'input_mode_conflict','Use the active interview input mode')
    if not session.current_question or session.current_question['id']!=payload['question_id']:
        raise DomainError(409,'question_conflict','Answer the current question')
    last_question=db.scalar(select(InterviewTurn).where(InterviewTurn.session_id==session.id,InterviewTurn.speaker=='interviewer',InterviewTurn.question_id==payload['question_id']).order_by(InterviewTurn.sequence.desc()))
    if last_question and last_question.delivery!='completed':raise DomainError(409,'question_not_delivered','Repeat the question before answering')
    session.revision+=1
    turn=add_turn(db,session,'learner',payload['text'],payload['question_id'],mode,payload['client_turn_id'],replacement_id=session.pending_replacement_id)
    session.pending_replacement_id=None
    if pending:
        pending.pending_utterance_id=None;pending.pending_question_id=None;pending.pending_since=None
    coverage=copy.deepcopy(session.coverage)
    item=coverage[session.current_index];item['answer_turn_ids'].append(turn.id)
    if plan.get('action')=='ask_follow_up' and item.get('follow_up_count',0)==0 and not session.current_question.get('is_follow_up'):
        prompt=str(plan.get('prompt','')).strip()
        if not prompt or len(prompt)>1500:raise DomainError(503,'invalid_interviewer_response','The interviewer could not prepare a valid follow-up')
        item['follow_up_count']=1
        question={'id':item['question_id']+'-follow-up','objective':item['objective'],'prompt':prompt,'is_follow_up':True,'parent_question_id':item['question_id']}
        session.coverage=coverage;deliver(db,session,question,settings)
    else:
        item['status']='answered';session.coverage=coverage;session.current_index+=1
        if session.current_index>=3:
            session.current_question=None;finish(db,session,settings,'questions_complete',increment_revision=False)
        else:
            release=db.get(Release,session.release_id);deliver(db,session,release.data['question_plan'][session.current_index],settings)
    state_event(db,session,settings)
    return {'session':public_session(db,session,settings),'turn':turn_dict(turn)}

@serialized_provider_call
def submit_answer(database,sid,actor,key,payload,plan=None):
    # Provider calls happen outside the write transaction. The transaction revalidates revision and current question.
    if plan is None:
        with database.session_factory() as db:
            session=owned(db,sid,actor)
            existing=db.scalar(select(InterviewTurn).where(InterviewTurn.session_id==sid,InterviewTurn.client_turn_id==payload['client_turn_id']))
            record=db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.actor_id==actor,IdempotencyRecord.route==f'/viva/sessions/{sid}/answers',IdempotencyRecord.key==key))
            should_plan=not existing and not record
            if should_plan:
                revision(session,payload['expected_revision']);check_active(session,database.settings)
                if session.input_mode!='text':raise DomainError(409,'input_mode_conflict','Use the active interview input mode')
                if not session.current_question or session.current_question['id']!=payload['question_id']:
                    raise DomainError(409,'question_conflict','Answer the current question')
        if should_plan:
            from .provider import plan_follow_up
            plan=plan_follow_up(answer_context(database,sid,actor,payload))
        else:plan={'action':'advance','prompt':None}
    return mutate(database,actor,f'/viva/sessions/{sid}/answers',key,payload,lambda db:accept_answer(db,owned(db,sid,actor,True),payload,database.settings,plan),sid)

def pause(db,session,payload,settings):
    revision(session,payload['expected_revision'])
    if session.status not in ('created','connecting','in_progress'):raise DomainError(409,'invalid_state','Only an active interview can be paused')
    pending=_pending_connection(db,session)
    pause_clock(session);session.status='paused';session.revision+=1
    _pending_interrupt(db,session,pending)
    session.connection_epoch+=1
    for conn in db.scalars(select(VoiceConnection).where(VoiceConnection.session_id==session.id,VoiceConnection.status.in_(['active','connecting']))):conn.status='closed'
    state_event(db,session,settings);return public_session(db,session,settings)

def resume(db,session,payload,settings):
    revision(session,payload['expected_revision'])
    if session.status not in ('paused','created'):raise DomainError(409,'invalid_state','Pause the interview before resuming or changing mode')
    session.input_mode=payload['input_mode'];session.revision+=1
    if payload['input_mode']=='text':
        session.status='in_progress';session.active_since=now()
        if session.current_question:deliver(db,session,session.current_question,settings)
    else:session.status='paused'
    state_event(db,session,settings);return public_session(db,session,settings)

def repeat(db,session,payload,settings):
    revision(session,payload['expected_revision']);check_active(session,settings)
    if not session.current_question or payload['question_id']!=session.current_question['id']:raise DomainError(409,'question_conflict','Only the current question can be repeated')
    session.revision+=1;deliver(db,session,session.current_question,settings);state_event(db,session,settings)
    return public_session(db,session,settings)

def flag(db,session,payload,settings):
    revision(session,payload['expected_revision']);check_active(session,settings)
    turn=db.get(InterviewTurn,payload['turn_id'])
    if not turn or turn.session_id!=session.id or turn.speaker!='learner':raise DomainError(404,'not_found','Learner turn not found')
    turn.quality_flag=payload['reason'];session.revision+=1
    if session.current_question and (turn.question_id==session.current_question['id'] or turn.question_id==session.current_question.get('parent_question_id')):
        session.pending_replacement_id=turn.id
    event(db,session,'turn_committed',{'turn':turn_dict(turn)});state_event(db,session,settings)
    return {'session':public_session(db,session,settings),'turn':turn_dict(turn),'replacement_requested':bool(session.pending_replacement_id)}

def voice_ready(database,sid,actor,epoch,provider_call_id):
    with _LOCK,database.session_factory.begin() as db:
        session=owned(db,sid,actor,True)
        if session.connection_epoch!=epoch or session.status!='connecting':raise DomainError(409,'connection_expired','Voice connection is no longer active')
        session.status='in_progress';session.active_since=now();session.revision+=1
        conn=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid,VoiceConnection.epoch==epoch))
        if conn:conn.status='active';conn.provider_call_id=provider_call_id;conn.lease_until=now()+timedelta(seconds=90)
        state_event(db,session,database.settings)
        return public_session(db,session,database.settings)

def mark_question_delivered(database,sid,actor,epoch,delivery='completed',turn_id=None):
    if delivery not in ('completed','interrupted','unconfirmed'):raise DomainError(422,'invalid_delivery','Unsupported delivery status')
    with _LOCK,database.session_factory.begin() as db:
        session=owned(db,sid,actor,True)
        if session.connection_epoch!=epoch or session.status!='in_progress' or not session.current_question:return False
        query=select(InterviewTurn).where(InterviewTurn.session_id==sid,InterviewTurn.speaker=='interviewer',InterviewTurn.question_id==session.current_question['id'])
        if turn_id is not None:query=query.where(InterviewTurn.id==turn_id)
        turn=db.scalar(query.order_by(InterviewTurn.sequence.desc()))
        if not turn:return False
        # Delivery is acknowledged for the captured spoken turn, never whichever turn is newest later.
        if turn_id is not None:
            latest=db.scalar(select(InterviewTurn.id).where(InterviewTurn.session_id==sid,InterviewTurn.speaker=='interviewer',InterviewTurn.question_id==session.current_question['id']).order_by(InterviewTurn.sequence.desc()))
            if latest!=turn.id:return False
        if turn.delivery==delivery:return True
        turn.delivery=delivery;event(db,session,'turn_committed',{'turn':turn_dict(turn)})
        return True


@serialized_provider_call
def commit_voice_turn(database,sid,actor,epoch,event_id,content):
    with database.session_factory() as db:
        session=owned(db,sid,actor)
        if session.connection_epoch!=epoch:raise DomainError(409,'connection_expired','Voice connection is no longer active')
        previous=db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.actor_id==actor,IdempotencyRecord.route==f'/voice/{sid}/{epoch}',IdempotencyRecord.key==event_id))
        if previous:
            if previous.response.get('turn',{}).get('text')!=content.strip():
                raise DomainError(409,'turn_conflict','A provider item ID cannot change its committed answer')
            return previous.response
        if not content.strip():return None
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid,VoiceConnection.epoch==epoch))
        if (not connection or connection.status!='active' or connection.pending_utterance_id!=event_id or
                not connection.pending_question_id or not session.current_question or
                connection.pending_question_id!=session.current_question['id']):
            raise DomainError(409,'utterance_not_accepted','Voice input was not accepted for this question')
        drain=_pending_state(connection)
        check_active(session,database.settings,allow_grace=drain['pending'] and drain['remaining_seconds']>0)
        payload={'expected_revision':session.revision,'question_id':connection.pending_question_id,'client_turn_id':f'voice:{epoch}:{event_id}','text':content.strip()}
    from .provider import plan_follow_up
    plan=plan_follow_up(answer_context(database,sid,actor,payload))
    def accept(db):
        session=owned(db,sid,actor,True)
        if session.connection_epoch!=epoch:raise DomainError(409,'connection_expired','Voice connection is no longer active')
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid,VoiceConnection.epoch==epoch))
        if (not connection or connection.status!='active' or connection.pending_utterance_id!=event_id or
                not connection.pending_question_id or connection.pending_question_id!=payload['question_id'] or
                not session.current_question or connection.pending_question_id!=session.current_question['id']):
            raise DomainError(409,'utterance_not_accepted','Voice input was not accepted for this question')
        return accept_answer(db,session,payload,database.settings,plan,'voice')
    return mutate(database,actor,f'/voice/{sid}/{epoch}',event_id,payload,accept,sid)

def voice_disconnected(database,sid,actor,epoch):
    with _LOCK,database.session_factory.begin() as db:
        session=owned(db,sid,actor,True)
        if session.connection_epoch==epoch and session.status in ('connecting','in_progress'):
            pause(db,session,{'expected_revision':session.revision},database.settings)

def sweep_expired(database):
    """Server-side timers start a bounded drain before freezing active voice input."""
    with _LOCK,database.session_factory.begin() as db:
        for session in db.scalars(select(VivaSession).where(VivaSession.status.in_(['created','connecting','in_progress','paused']))):
            timed_out=elapsed(session)>=database.settings.max_active_seconds
            expired=now()>=aware(session.expires_at)
            if not timed_out and not expired:continue
            connection=_pending_connection(db,session)
            if connection:
                if not connection.drain_started_at:connection.drain_started_at=now()
                if _pending_state(connection)['remaining_seconds']>0:continue
            finish(db,session,database.settings,'time_limit' if timed_out else 'expired')
