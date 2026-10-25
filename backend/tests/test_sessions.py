"""Behavior-first contract tests for release-bound technical Viva sessions."""
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app

@pytest.fixture
def client(tmp_path):
    settings = Settings(database_url='sqlite:///' + str(tmp_path / 'test.db'), provider_mode='fixture', run_inline_worker=False)
    with TestClient(create_app(settings)) as value:
        yield value

def headers(token='demo-learner-token', key=None):
    return {'Authorization': 'Bearer ' + token, 'Idempotency-Key': key or str(uuid4())}

def start(client, mode='text', attempt='attempt-demo-success', key=None):
    return client.post('/api/v1/viva/sessions', json={'attempt_id': attempt, 'input_mode': mode}, headers=headers(key=key))

def test_owned_completed_attempt_starts_release_bound_session(client):
    response = start(client)
    assert response.status_code == 200
    session = response.json()
    assert session['release_id'] == 'release-demo-v1'
    assert session['status'] == 'in_progress'
    assert session['current_question']['objective'] == 'explanation'
    assert session['question_count'] == 3
    assert 'rubric' not in session and 'accepted_answers' not in str(session)
    again = start(client).json()
    assert again['id'] == session['id']

def test_ownership_and_auth(client):
    assert client.get('/api/v1/bootstrap').status_code == 401
    assert start(client, attempt='attempt-other').status_code == 404
    session = start(client).json()
    assert client.get('/api/v1/viva/sessions/' + session['id'], headers=headers('demo-other-token')).status_code == 404

def test_deduplication_precedes_stale_revision(client):
    session = start(client).json()
    payload = {'expected_revision': session['revision'], 'question_id': session['current_question']['id'], 'client_turn_id': 'answer-1', 'text': 'I inspected the housing and compared the observations before choosing a check.'}
    url = '/api/v1/viva/sessions/' + session['id'] + '/answers'
    first = client.post(url, json=payload, headers=headers(key='same'))
    assert first.status_code == 202
    assert client.post(url, json=payload, headers=headers(key='same')).json() == first.json()
    changed = dict(payload, text='different')
    assert client.post(url, json=changed, headers=headers(key='same')).status_code == 409
    assert client.post(url, json=payload, headers=headers(key='new')).status_code == 202
    assert client.post(url, json=dict(payload, client_turn_id='new'), headers=headers()).status_code == 409

def test_pause_resume_and_early_finish_are_recoverable(client):
    session = start(client).json()
    base = '/api/v1/viva/sessions/' + session['id']
    paused = client.post(base + '/pause', json={'expected_revision': session['revision']}, headers=headers()).json()
    assert paused['status'] == 'paused'
    resumed = client.post(base + '/resume', json={'expected_revision': paused['revision'], 'input_mode':'text'}, headers=headers()).json()
    assert resumed['status'] == 'in_progress'
    job = client.post(base + '/finish', json={'expected_revision': resumed['revision']}, headers=headers())
    assert job.status_code == 202
    assert client.post(base + '/finish', json={'expected_revision': 0}, headers=headers()).json()['id'] == job.json()['id']
    assert client.get(base + '/result', headers=headers()).status_code == 409
    assert client.post(base + '/answers', json={'expected_revision':0,'question_id':'x','client_turn_id':'x','text':'late'},headers=headers()).status_code == 409

def test_events_are_ordered_and_current_question_is_restorable(client):
    session = start(client).json()
    base = '/api/v1/viva/sessions/' + session['id']
    events = client.get(base+'/events?limit=1',headers=headers()).json()
    assert len(events['events']) == 1
    assert events['has_more']
    second = client.get(base+'/events?after='+str(events['next_after']),headers=headers()).json()
    assert all(e['sequence'] > events['next_after'] for e in second['events'])
    restored = client.get(base,headers=headers()).json()
    assert restored['current_question'] == session['current_question']
    assert len(restored['transcript']) == 1

def test_release_sources_require_scope(client):
    assert client.get('/api/v1/studio/releases/release-demo-v1',headers=headers()).status_code == 200
    assert client.get('/api/v1/studio/releases/release-demo-v1/documents/doc-inspection/content',headers=headers()).status_code == 200
    assert client.get('/api/v1/studio/releases/release-demo-v1/documents/unknown/content',headers=headers()).status_code == 404

def test_question_and_followup_limits_complete_automatically(client):
    session = start(client).json()
    base = '/api/v1/viva/sessions/' + session['id']
    turns = 0
    while session['status'] == 'in_progress':
        turns += 1
        assert turns <= 6
        reply = client.post(base+'/answers',headers=headers(),json={'expected_revision':session['revision'],'question_id':session['current_question']['id'],'client_turn_id':str(turns),'text':'The observed evidence informs the check. If it is insufficient or inconsistent I stop and escalate for supervision.'})
        assert reply.status_code == 202, reply.text
        session = reply.json()['session']
    assert session['status'] == 'evaluating'
    assert session['answered_count'] == 3
    assert session['follow_up_count'] <= 3

def test_voice_semantic_event_replay_creates_one_answer(client):
    from app import service
    database,sid=setup_live_voice_session(client)
    service.voice_input_started(database,sid,'learner-1',1,'provider-item')
    first=service.commit_voice_turn(database,sid,'learner-1',1,'provider-item','I gathered observations before choosing a check.')
    replay=service.commit_voice_turn(database,sid,'learner-1',1,'provider-item','I gathered observations before choosing a check.')
    assert replay==first
    restored=client.get('/api/v1/viva/sessions/'+sid,headers=headers()).json()
    assert len([t for t in restored['transcript'] if t['speaker']=='learner'])==1
    with pytest.raises(service.DomainError) as error:
        service.commit_voice_turn(database,sid,'learner-1',1,'provider-item','A changed transcript must not replace committed input silently.')
    assert error.value.status==409
    assert error.value.code=='turn_conflict'

def test_flagged_turn_keeps_original_and_links_replacement(client):
    session=start(client).json();base='/api/v1/viva/sessions/'+session['id']
    first=client.post(base+'/answers',headers=headers(),json={'expected_revision':session['revision'],'question_id':session['current_question']['id'],'client_turn_id':'first','text':'An unclear initial answer.'}).json()
    session=first['session'];original=first['turn']['id']
    flagged=client.post(base+'/transcript-flags',headers=headers(),json={'expected_revision':session['revision'],'turn_id':original,'reason':'Transcription did not match my intended answer.'})
    assert flagged.status_code==200,flagged.text
    assert flagged.json()['replacement_requested'] is True
    session=flagged.json()['session']
    answer=client.post(base+'/answers',headers=headers(),json={'expected_revision':session['revision'],'question_id':session['current_question']['id'],'client_turn_id':'replacement','text':'My observation supports the check; missing evidence requires escalation.'})
    assert answer.status_code==202,answer.text
    assert answer.json()['turn']['replacement_id']==original
    original_turn=next(t for t in answer.json()['session']['transcript'] if t['id']==original)
    assert original_turn['quality_flag']

def test_reset_reseed_reuses_start_key_without_stale_session(client):
    from app.cli import reset_demo
    from app.seed import seed
    database=client.app.state.db
    first=start(client,key='reset-key').json()
    counts=reset_demo(database,confirm=True)
    assert counts['viva_sessions']==1
    seed(database)
    second=start(client,key='reset-key')
    assert second.status_code==200,second.text
    assert second.json()['id']!=first['id']
    assert second.json()['bundle_hash']==first['bundle_hash']

def test_server_timer_freezes_timed_out_interview(client):
    from datetime import timedelta
    from app import service
    from app.models import VivaSession, now
    session=start(client).json();database=client.app.state.db
    with database.session_factory.begin() as db:
        row=db.get(VivaSession,session['id']);row.active_since=now()-timedelta(seconds=601)
    service.sweep_expired(database)
    updated=client.get('/api/v1/viva/sessions/'+session['id'],headers=headers()).json()
    assert updated['status']=='evaluating'
    assert updated['evaluation_job_id']

def setup_live_voice_session(client):
    from app import service
    from app.models import VivaSession, VoiceConnection, uid
    database=client.app.state.db
    session=start(client,mode='voice').json()
    with database.session_factory.begin() as db:
        row=db.get(VivaSession,session['id']);row.status='connecting';row.connection_epoch=1
        db.add(VoiceConnection(id=uid(),session_id=row.id,epoch=1,status='connecting'))
    service.voice_ready(database,session['id'],'learner-1',1,'fake-call')
    service.mark_question_delivered(database,session['id'],'learner-1',1)
    return database,session['id']


def test_pending_utterance_delays_timer_but_not_beyond_ten_seconds(client):
    from datetime import timedelta
    from app import service
    from app.models import VivaSession, VoiceConnection, now
    from sqlalchemy import select
    database,sid=setup_live_voice_session(client)
    service.voice_input_started(database,sid,'learner-1',1,'utterance-1')
    with database.session_factory.begin() as db:
        session=db.get(VivaSession,sid);session.active_since=now()-timedelta(seconds=601)
    service.sweep_expired(database)
    with database.session_factory() as db:
        assert db.get(VivaSession,sid).status=='in_progress'
    with database.session_factory.begin() as db:
        conn=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid,VoiceConnection.epoch==1))
        conn.drain_started_at=now()-timedelta(seconds=11)
    service.sweep_expired(database)
    with database.session_factory() as db:
        session=db.get(VivaSession,sid)
        assert session.status=='evaluating'
        turns=service.turns_for(db,sid)
        assert any(t['speaker']=='learner' and t['delivery']=='interrupted' for t in turns)


def test_pending_utterance_can_finish_during_time_limit_grace(client):
    from datetime import timedelta
    from app import service
    from app.models import VivaSession, now
    database,sid=setup_live_voice_session(client)
    service.voice_input_started(database,sid,'learner-1',1,'utterance-1')
    with database.session_factory.begin() as db:
        db.get(VivaSession,sid).active_since=now()-timedelta(seconds=601)
    result=service.commit_voice_turn(database,sid,'learner-1',1,'utterance-1','I use recorded observations and escalate inconsistent evidence.')
    assert result['turn']['delivery']=='completed'
    service.voice_input_finished(database,sid,'learner-1',1,'utterance-1')
    assert service.pending_voice_input(database,sid,'learner-1')['pending'] is False


def test_repeated_vad_event_does_not_extend_grace(client):
    from datetime import timedelta
    from app import service
    from app.models import VoiceConnection, now
    from sqlalchemy import select
    database,sid=setup_live_voice_session(client)
    service.voice_input_started(database,sid,'learner-1',1,'utterance-1')
    with database.session_factory.begin() as db:
        conn=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid,VoiceConnection.epoch==1))
        timestamp=now()-timedelta(seconds=7);conn.drain_started_at=timestamp
    service.voice_input_started(database,sid,'learner-1',1,'utterance-1')
    pending=service.pending_voice_input(database,sid,'learner-1')
    assert pending['remaining_seconds']<4


def test_finish_requires_pending_utterance_drain(client):
    from app import service
    database,sid=setup_live_voice_session(client)
    service.voice_input_started(database,sid,'learner-1',1,'utterance-1')
    with database.session_factory.begin() as db:
        row=service.owned(db,sid,'learner-1')
        with pytest.raises(service.DomainError) as error:service.finish(db,row,database.settings)
        assert error.value.code=='utterance_in_progress'


def test_late_delivery_ack_does_not_mark_new_question_delivered(client):
    from app import service
    from app.models import InterviewTurn
    from sqlalchemy import select
    database,sid=setup_live_voice_session(client)
    with database.session_factory() as db:
        original=db.scalar(select(InterviewTurn).where(InterviewTurn.session_id==sid,InterviewTurn.speaker=='interviewer'))
        original_id=original.id
    service.voice_input_started(database,sid,'learner-1',1,'answer-item')
    result=service.commit_voice_turn(database,sid,'learner-1',1,'answer-item','My observed evidence supports the check.')
    follow_up=result['session']['current_question']
    assert follow_up['is_follow_up']
    assert service.mark_question_delivered(database,sid,'learner-1',1,'completed',turn_id=original_id) is False
    with database.session_factory() as db:
        latest=db.scalar(select(InterviewTurn).where(InterviewTurn.session_id==sid,InterviewTurn.speaker=='interviewer').order_by(InterviewTurn.sequence.desc()))
        assert latest.delivery=='unconfirmed'


def test_long_answer_gets_drain_grace_after_finish_not_at_speech_start(client):
    from datetime import timedelta
    from app import service
    from app.models import VoiceConnection, now
    from sqlalchemy import select
    database,sid=setup_live_voice_session(client)
    service.voice_input_started(database,sid,'learner-1',1,'long-utterance')
    with database.session_factory.begin() as db:
        conn=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid,VoiceConnection.epoch==1))
        conn.pending_since=now()-timedelta(seconds=45)
    pending=service.pending_voice_input(database,sid,'learner-1',True)
    assert pending['pending'] is True
    assert pending['remaining_seconds']>9
    with database.session_factory.begin() as db:
        row=service.owned(db,sid,'learner-1')
        with pytest.raises(service.DomainError) as error:service.finish(db,row,database.settings)
        assert error.value.code=='utterance_in_progress'


def test_pausing_pending_speech_records_interrupted_turn(client):
    from app import service
    database,sid=setup_live_voice_session(client)
    service.voice_input_started(database,sid,'learner-1',1,'paused-utterance')
    with database.session_factory.begin() as db:
        row=service.owned(db,sid,'learner-1')
        service.pause(db,row,{'expected_revision':row.revision},database.settings)
    restored=client.get('/api/v1/viva/sessions/'+sid,headers=headers()).json()
    assert restored['status']=='paused'
    assert any(t['speaker']=='learner' and t['delivery']=='interrupted' for t in restored['transcript'])


def test_overlapping_voice_items_cannot_answer_follow_up(client,monkeypatch):
    from app import service, provider
    from app.models import VivaSession, VoiceConnection, ProviderReceipt
    from sqlalchemy import select
    database,sid=setup_live_voice_session(client)
    first=service.voice_input_started(database,sid,'learner-1',1,'A')
    assert service.voice_input_started(database,sid,'learner-1',1,'B')['id']=='A'
    with database.session_factory() as db:
        question=db.get(VivaSession,sid).current_question['id']
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid))
        assert connection.pending_question_id==question
        assert connection.pending_utterance_id==first['id']
        receipts=list(db.scalars(select(ProviderReceipt).where(ProviderReceipt.session_id==sid)))
        assert {(r.event_id,r.semantic_key) for r in receipts}=={('A','A'),('B','B')}
    result=service.commit_voice_turn(database,sid,'learner-1',1,'A','My observed evidence supports the check.')
    assert result['turn']['question_id']==question
    assert result['session']['current_question']['is_follow_up']
    before=client.get('/api/v1/viva/sessions/'+sid,headers=headers()).json()
    with database.session_factory() as db:
        coverage=db.get(VivaSession,sid).coverage
    def no_planning(context):
        pytest.fail('Rejected overlap must not call provider')
    monkeypatch.setattr(provider,'plan_follow_up',no_planning)
    with pytest.raises(service.DomainError) as error:
        service.commit_voice_turn(database,sid,'learner-1',1,'B','A late second answer.')
    assert error.value.code=='utterance_not_accepted'
    assert error.value.status==409
    assert service.voice_input_started(database,sid,'learner-1',1,'B')['pending'] is False
    assert service.voice_input_started(database,sid,'learner-1',1,'A')['pending'] is False
    after=client.get('/api/v1/viva/sessions/'+sid,headers=headers()).json()
    for field in ('revision','transcript','answered_count','follow_up_count','current_question'):
        assert after[field]==before[field]
    with database.session_factory() as db:
        assert db.get(VivaSession,sid).coverage==coverage
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid))
        assert connection.pending_utterance_id is None
        assert connection.pending_question_id is None
        assert connection.pending_since is None


def test_unstarted_voice_finalization_never_calls_provider(client,monkeypatch):
    from app import service, provider
    database,sid=setup_live_voice_session(client)
    before=client.get('/api/v1/viva/sessions/'+sid,headers=headers()).json()
    monkeypatch.setattr(provider,'plan_follow_up',lambda context:pytest.fail('Unstarted input reached provider'))
    with pytest.raises(service.DomainError) as error:
        service.commit_voice_turn(database,sid,'learner-1',1,'never-started','Unaccepted answer.')
    assert error.value.code=='utterance_not_accepted'
    after=client.get('/api/v1/viva/sessions/'+sid,headers=headers()).json()
    assert after['transcript']==before['transcript']
    assert after['revision']==before['revision']
    assert service.commit_voice_turn(database,sid,'learner-1',1,'never-started','  ') is None


def test_rejected_overlap_finalization_preserves_pending_owner_and_deadline(client,monkeypatch):
    from app import service, provider
    from app.models import VoiceConnection
    from sqlalchemy import select
    database,sid=setup_live_voice_session(client)
    service.voice_input_started(database,sid,'learner-1',1,'A')
    service.voice_input_started(database,sid,'learner-1',1,'B')
    service.pending_voice_input(database,sid,'learner-1',True)
    with database.session_factory() as db:
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid))
        captured=(connection.pending_question_id,connection.pending_since,connection.drain_started_at)
    monkeypatch.setattr(provider,'plan_follow_up',lambda context:pytest.fail('Rejected B reached provider'))
    with pytest.raises(service.DomainError) as error:
        service.commit_voice_turn(database,sid,'learner-1',1,'B','B finalized first.')
    assert error.value.code=='utterance_not_accepted'
    assert service.voice_input_finished(database,sid,'learner-1',1,'B') is False
    assert service.voice_input_finished(database,sid,'learner-1',2,'A') is False
    assert service.voice_input_started(database,sid,'learner-1',1,'A')['id']=='A'
    with database.session_factory() as db:
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid))
        assert connection.pending_utterance_id=='A'
        assert (connection.pending_question_id,connection.pending_since,connection.drain_started_at)==captured


@pytest.mark.parametrize('change,code',[
    ('question','utterance_not_accepted'),('epoch','connection_expired'),
    ('pending','utterance_not_accepted'),('captured_question','utterance_not_accepted'),
    ('connection','utterance_not_accepted'),('revision','stale_revision'),
    ('state','invalid_state'),('delivery','question_not_delivered'),
])
def test_voice_commit_rechecks_state_after_planning(client,monkeypatch,change,code):
    from app import service, provider
    from app.models import VivaSession, VoiceConnection, InterviewTurn
    from sqlalchemy import select
    database,sid=setup_live_voice_session(client)
    service.voice_input_started(database,sid,'learner-1',1,'A')
    with database.session_factory() as db:
        before=db.get(VivaSession,sid)
        coverage=before.coverage
        turns=service.turns_for(db,sid)
    def plan(context):
        with database.session_factory.begin() as db:
            session=db.get(VivaSession,sid)
            connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid))
            if change=='question':session.current_question=dict(session.current_question,id='other')
            elif change=='epoch':session.connection_epoch+=1
            elif change=='pending':connection.pending_utterance_id='B'
            elif change=='captured_question':connection.pending_question_id='other'
            elif change=='connection':connection.status='closed'
            elif change=='revision':session.revision+=1
            elif change=='state':session.status='paused'
            elif change=='delivery':
                db.scalar(select(InterviewTurn).where(InterviewTurn.session_id==sid)).delivery='unconfirmed'
        return {'action':'advance','prompt':None}
    monkeypatch.setattr(provider,'plan_follow_up',plan)
    with pytest.raises(service.DomainError) as error:
        service.commit_voice_turn(database,sid,'learner-1',1,'A','An answer accepted before planning.')
    assert error.value.code==code
    with database.session_factory() as db:
        assert db.get(VivaSession,sid).coverage==coverage
        assert [(t['id'],t['text']) for t in service.turns_for(db,sid)]==[(t['id'],t['text']) for t in turns]


@pytest.mark.parametrize('legacy',[False,True])
def test_interruption_uses_captured_question_not_current_question(client,legacy):
    from app import service
    from app.models import VivaSession, VoiceConnection
    from sqlalchemy import select
    database,sid=setup_live_voice_session(client)
    service.voice_input_started(database,sid,'learner-1',1,'A')
    with database.session_factory.begin() as db:
        session=db.get(VivaSession,sid)
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid))
        captured=connection.pending_question_id
        if legacy:connection.pending_question_id=None
        session.current_question=dict(session.current_question,id='new-question')
    with pytest.raises(service.DomainError) as error:
        service.commit_voice_turn(database,sid,'learner-1',1,'A','Must not answer another question.')
    assert error.value.code=='utterance_not_accepted'
    with database.session_factory.begin() as db:
        session=db.get(VivaSession,sid)
        service.pause(db,session,{'expected_revision':session.revision},database.settings)
    with database.session_factory() as db:
        turn=next(t for t in service.turns_for(db,sid) if t['speaker']=='learner')
        assert turn['delivery']=='interrupted'
        assert turn['question_id']==(None if legacy else captured)
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid))
        assert connection.pending_utterance_id is None
        assert connection.pending_question_id is None
        assert connection.pending_since is None


def test_voice_start_without_question_rejects_without_receipt(client):
    from app import service
    from app.models import VivaSession, ProviderReceipt
    from sqlalchemy import select
    database,sid=setup_live_voice_session(client)
    with database.session_factory.begin() as db:
        db.get(VivaSession,sid).current_question=None
    with pytest.raises(service.DomainError) as error:
        service.voice_input_started(database,sid,'learner-1',1,'A')
    assert error.value.code=='question_conflict'
    with database.session_factory() as db:
        assert db.scalar(select(ProviderReceipt).where(ProviderReceipt.session_id==sid)) is None


@pytest.mark.parametrize('client_id,question_id',[
    ('voice:1:B',None),('voice:2:A',None),('voice:1:A','wrong-question'),
])
def test_voice_grace_does_not_cover_another_item_or_question(client,client_id,question_id):
    from datetime import timedelta
    from app import service
    from app.models import VivaSession, VoiceConnection, now
    from sqlalchemy import select
    database,sid=setup_live_voice_session(client)
    service.voice_input_started(database,sid,'learner-1',1,'A')
    service.pending_voice_input(database,sid,'learner-1',True)
    with database.session_factory.begin() as db:
        session=db.get(VivaSession,sid)
        session.active_since=now()-timedelta(seconds=601)
    with pytest.raises(service.DomainError) as error:
        with database.session_factory.begin() as db:
            session=db.get(VivaSession,sid)
            payload={'expected_revision':session.revision,'question_id':question_id or session.current_question['id'],
                'client_turn_id':client_id,'text':'Cannot borrow another utterance grace.'}
            service.accept_answer(db,session,payload,database.settings,{'action':'advance'},'voice')
    assert error.value.code=='time_limit'
    with database.session_factory() as db:
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid))
        assert connection.pending_utterance_id=='A'
        assert connection.pending_question_id==db.get(VivaSession,sid).current_question['id']


def test_matching_voice_acceptance_clears_owner_but_preserves_drain(client):
    from app import service
    from app.models import VoiceConnection
    from sqlalchemy import select
    database,sid=setup_live_voice_session(client)
    service.voice_input_started(database,sid,'learner-1',1,'A')
    service.pending_voice_input(database,sid,'learner-1',True)
    with database.session_factory() as db:
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid))
        deadline=connection.drain_started_at
        question=connection.pending_question_id
    result=service.commit_voice_turn(database,sid,'learner-1',1,'A','My observations support the check.')
    assert result['turn']['question_id']==question
    with database.session_factory() as db:
        connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid))
        assert connection.pending_utterance_id is None
        assert connection.pending_question_id is None
        assert connection.pending_since is None
        assert connection.drain_started_at==deadline
    assert service.voice_input_finished(database,sid,'learner-1',1,'A') is False
