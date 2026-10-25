"""Server-owned Realtime WebRTC negotiation and sideband lifetime.

Only a persistent API process may own a sideband. Events from replaced epochs
cannot mutate the transcript. No provider credential is ever returned to clients.
"""
import asyncio
import json
import time
from collections import deque
from datetime import timedelta
from urllib.parse import urlparse, quote
from sqlalchemy import select, text
from . import service
from .models import VoiceConnection, VivaSession, IdempotencyRecord, InterviewTurn, uid, now
from .provider import ProviderUnavailable
from .voice_protocol import normalize_event, voice_session_config, question_response

_TASKS={}
_READY={}
_PENDING={}
_DRAINING={}


async def negotiate(database,sid,actor,payload,settings,key):
    if not key or len(key)>128:raise service.DomainError(422,'idempotency_key_required','Provide an Idempotency-Key')
    route=f'/viva/sessions/{sid}/connection';hash_value=service.digest(payload)
    with service._LOCK,database.session_factory.begin() as db:
        session=service.owned(db,sid,actor,True)
        replay=db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.actor_id==actor,IdempotencyRecord.route==route,IdempotencyRecord.key==key))
        if replay:
            if replay.request_hash!=hash_value:raise service.DomainError(409,'idempotency_conflict','The idempotency key was used with another payload')
            return replay.response
        if settings.provider_mode!='openai' or not settings.openai_api_key:
            raise service.DomainError(503,'voice_unavailable','Live voice requires a configured provider. Use text mode for the offline fixture.')
        service.revision(session,payload['expected_revision'])
        other=db.scalar(select(VoiceConnection.id).join(VivaSession,VoiceConnection.session_id==VivaSession.id).where(
            VivaSession.learner_id==actor,VoiceConnection.session_id!=sid,VoiceConnection.status.in_(['active','connecting'])))
        if other:raise service.DomainError(409,'connection_conflict','Pause the other voice interview before connecting')
        if session.status not in ('created','paused') or session.input_mode!='voice':
            raise service.DomainError(409,'invalid_state','Pause or create a voice interview before connecting')
        if now()>=service.aware(session.expires_at):raise service.DomainError(409,'session_expired','Interview expired')
        session.connection_epoch+=1;epoch=session.connection_epoch
        session.status='connecting';session.revision+=1
        connection=VoiceConnection(id=uid(),session_id=sid,epoch=epoch,status='connecting',created_at=now(),lease_until=now()+timedelta(seconds=90))
        db.add(connection);connection_id=connection.id
        service.state_event(db,session,settings)
    reservation=None
    socket=None
    handed_off=False
    try:
        from . import budget
        with database.session_factory() as db:
            current=service.owned(db,sid,actor)
            seconds=max(0,settings.max_active_seconds-service.elapsed(current))+10
        reservation=budget.reserve(database,sid,'audio',budget.estimate_audio(settings,seconds))
        import httpx
        import websockets
        if not settings.provider_base_url.startswith('https://'):
            raise ProviderUnavailable('Provider endpoint must use HTTPS')
        async with httpx.AsyncClient(timeout=settings.provider_timeout) as client:
            response=await client.post(settings.provider_base_url.rstrip('/')+'/realtime/calls',
                headers={'Authorization':'Bearer '+settings.openai_api_key},
                files={'sdp':(None,payload['sdp_offer']),'session':(None,json.dumps(voice_session_config(settings.voice_model)))})
            response.raise_for_status()
            call_id=response.headers.get('location','').rstrip('/').split('/')[-1]
            if not call_id or len(call_id)>200 or not response.text:
                raise ProviderUnavailable('Provider returned no call identifier')
            parsed=urlparse(settings.provider_base_url)
            endpoint='wss://'+parsed.netloc+parsed.path.rstrip('/')+'/realtime?call_id='+quote(call_id,safe='')
            # websockets >=14 API. Keep credentials exclusively in backend headers.
            socket=await websockets.connect(endpoint,additional_headers={'Authorization':'Bearer '+settings.openai_api_key},open_timeout=settings.provider_timeout,max_size=2_000_000)
        with database.session_factory.begin() as db:
            conn=db.get(VoiceConnection,connection_id)
            conn.provider_call_id=call_id
        ready=asyncio.Event()
        _READY[connection_id]=ready
        task=asyncio.create_task(_sideband(socket,database,sid,actor,epoch,reservation,ready))
        handed_off=True
        _TASKS[connection_id]=task
        def cleanup(_):
            _TASKS.pop(connection_id,None);_READY.pop(connection_id,None)
        task.add_done_callback(cleanup)
        with service._LOCK,database.session_factory.begin() as db:
            session=service.owned(db,sid,actor,True)
            result={'connection_id':connection_id,'sdp_answer':response.text,'session':service.public_session(db,session,settings)}
            db.add(IdempotencyRecord(actor_id=actor,route=route,key=key,request_hash=hash_value,response=result,session_id=sid))
        return result
    except service.DomainError:
        if socket is not None and not handed_off:await socket.close()
        if reservation is not None and not handed_off:
            budget.settle(reservation,{'outcome':'connection_not_started'},0)
        await asyncio.to_thread(service.voice_disconnected,database,sid,actor,epoch)
        raise
    except Exception:
        if socket is not None and not handed_off:await socket.close()
        if reservation is not None and not handed_off:
            budget.settle(reservation,{'outcome':'connection_not_started'},0)
        await asyncio.to_thread(service.voice_disconnected,database,sid,actor,epoch)
        raise service.DomainError(503,'voice_unavailable','Voice setup failed. Progress is saved; retry or switch to text.') from None


def _connection_state(database,sid,actor,epoch):
    with database.session_factory.begin() as db:
        session=service.owned(db,sid,actor)
        if session.connection_epoch!=epoch or session.status!='in_progress':return None
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid,VoiceConnection.epoch==epoch))
        if connection:connection.lease_until=now()+timedelta(seconds=90)
        latest=db.scalar(select(InterviewTurn).where(InterviewTurn.session_id==sid,InterviewTurn.speaker=='interviewer').order_by(InterviewTurn.sequence.desc()))
        return {'turn_id':latest.id if latest else None,'question':session.current_question}


async def ready_connection(database,sid,actor,payload,key):
    connection_id=payload['connection_id']
    with database.session_factory() as db:
        service.owned(db,sid,actor)
        previous=db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.actor_id==actor,IdempotencyRecord.route==f'/viva/sessions/{sid}/connection/ready',IdempotencyRecord.key==key))
        if previous:
            if previous.request_hash!=service.digest(payload):raise service.DomainError(409,'idempotency_conflict','The idempotency key was used with another payload')
            return previous.response
    ready=_READY.get(connection_id)
    if ready is None:raise service.DomainError(409,'connection_expired','Voice connection is no longer owned by this process')
    def accept(db):
        session=service.owned(db,sid,actor,True)
        connection=db.get(VoiceConnection,connection_id)
        if not connection or connection.session_id!=sid or connection.epoch!=session.connection_epoch:
            raise service.DomainError(409,'connection_expired','Voice connection is no longer active')
        service.revision(session,payload['expected_revision'])
        if session.status!='connecting':raise service.DomainError(409,'invalid_state','Voice connection is not awaiting media readiness')
        session.status='in_progress';session.active_since=now();session.revision+=1
        connection.status='active';connection.lease_until=now()+timedelta(seconds=90)
        service.state_event(db,session,database.settings)
        return service.public_session(db,session,database.settings)
    result=await asyncio.to_thread(service.mutate,database,actor,f'/viva/sessions/{sid}/connection/ready',key,payload,accept,sid)
    ready.set()
    return result


async def drain_pending(database,sid,actor):
    """Close new answer acceptance and await an already accepted voice utterance."""
    pending=await asyncio.to_thread(service.pending_voice_input,database,sid,actor,True)
    _DRAINING[sid]=pending.get('id') if pending.get('pending') else None
    event=_PENDING.get(sid)
    if pending.get('pending') and event is not None:
        try:await asyncio.wait_for(event.wait(),timeout=max(.01,pending['remaining_seconds']))
        except asyncio.TimeoutError:pass
    return bool(pending.get('pending'))


async def _sideband(socket,database,sid,actor,epoch,reservation,ready):
    started=None
    send_lock=asyncio.Lock()
    state={'spoken':None,'speaking':False,'response_id':None,'generation':0}
    awaiting=deque();responses={}
    pending_done=asyncio.Event();pending_done.set()
    _PENDING[sid]=pending_done
    async def send(event):
        async with send_lock:await socket.send(json.dumps(event))
    async def questions():
        await asyncio.wait_for(ready.wait(),timeout=30)
        while True:
            current=await asyncio.to_thread(_connection_state,database,sid,actor,epoch)
            if current is None:
                await socket.close();return
            if current['question'] and current['turn_id']!=state['spoken'] and sid not in _DRAINING:
                if state['speaking']:
                    await send({'type':'response.cancel'})
                # A late playback event from a superseded response cannot complete a repeat.
                responses.clear()
                state['spoken']=current['turn_id'];state['speaking']=True
                state['generation']+=1
                awaiting.append((current['turn_id'],state['generation']))
                await send(question_response(current['question']['prompt']))
            await asyncio.sleep(.25)
    async def ready_timeout():
        try:await asyncio.wait_for(ready.wait(),timeout=30)
        except asyncio.TimeoutError:await socket.close()
    pump=asyncio.create_task(questions())
    timeout_task=asyncio.create_task(ready_timeout())
    try:
        async for raw in socket:
            if started is None and ready.is_set():started=time.monotonic()
            if len(raw)>2_000_000:continue
            event=normalize_event(json.loads(raw))
            if not event:continue
            kind=event['kind']
            if kind=='response_created' and event.get('response_id') and awaiting:
                turn,generation=awaiting.popleft()
                if generation==state['generation']:
                    responses[event['response_id']]=turn;state['response_id']=event['response_id']
            elif kind in ('delivered','interrupted'):
                identity=event.get('response_id');turn=responses.get(identity)
                if turn is not None:
                    await asyncio.to_thread(service.mark_question_delivered,database,sid,actor,epoch,
                        'completed' if kind=='delivered' else 'interrupted',turn)
                    if identity==state['response_id']:state['speaking']=False
            elif kind=='interruption':
                item=event.get('item_id')
                if item and sid not in _DRAINING:
                    pending=await asyncio.to_thread(service.voice_input_started,database,sid,actor,epoch,item)
                    if pending['pending'] and pending['id']==item:pending_done.clear()
                if state['speaking']:
                    turn=responses.get(state['response_id'])
                    if turn is not None:await asyncio.to_thread(service.mark_question_delivered,database,sid,actor,epoch,'interrupted',turn)
                    state['speaking']=False
                    await send({'type':'response.cancel'})
            elif kind=='answer':
                if sid in _DRAINING and _DRAINING[sid]!=event['id']:continue
                try:
                    result=await asyncio.to_thread(service.commit_voice_turn,database,sid,actor,epoch,event['id'],event['text'])
                    if result and result['session']['status']=='evaluating':break
                except service.DomainError as exc:
                    if exc.code=='question_not_delivered':state['spoken']=None
                    elif exc.code in ('connection_expired','time_limit','session_expired','budget_exhausted'):break
                except ProviderUnavailable:break
                finally:
                    await asyncio.to_thread(service.voice_input_finished,database,sid,actor,epoch,event['id'])
                    pending=await asyncio.to_thread(service.pending_voice_input,database,sid,actor)
                    if not pending['pending']:pending_done.set()
            elif kind=='error':break
    except (asyncio.CancelledError,Exception):
        pass
    finally:
        pump.cancel();timeout_task.cancel()
        await asyncio.gather(pump,timeout_task,return_exceptions=True)
        await socket.close()
        from . import budget
        seconds=max(0,time.monotonic()-started) if started is not None else 0
        try:
            budget.settle(reservation,{'audio_seconds':round(seconds,3),'cost_basis':'configured_upper_bound_per_minute'},budget.estimate_audio(database.settings,seconds))
        finally:
            pending_done.set()
            if _PENDING.get(sid) is pending_done:
                _PENDING.pop(sid,None);_DRAINING.pop(sid,None)
            await asyncio.to_thread(service.voice_disconnected,database,sid,actor,epoch)


async def close_session(database,sid):
    with database.session_factory() as db:
        identities=list(db.scalars(select(VoiceConnection.id).where(VoiceConnection.session_id==sid)))
    tasks=[_TASKS[identity] for identity in identities if identity in _TASKS and _TASKS[identity] is not asyncio.current_task()]
    for task in tasks:task.cancel()
    if tasks:await asyncio.gather(*tasks,return_exceptions=True)


async def shutdown():
    tasks=list(_TASKS.values())
    for task in tasks:task.cancel()
    if tasks:await asyncio.gather(*tasks,return_exceptions=True)
