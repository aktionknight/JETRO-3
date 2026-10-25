"""Rejected overlapping speech must not release the accepted speech drain."""
import asyncio
import json
from uuid import uuid4
from sqlalchemy import select
from app import service, voice, budget
from app.config import Settings
from app.db import Database
from app.models import VivaSession, VoiceConnection, uid
from app.seed import seed


class Socket:
    def __init__(self):
        self.events=asyncio.Queue()
        self.processed=None
        self.closed=False

    def __aiter__(self):return self

    async def __anext__(self):
        if self.processed is not None:self.processed.set_result(None)
        raw,self.processed=await self.events.get()
        return raw

    async def emit(self,event):
        processed=asyncio.get_running_loop().create_future()
        await self.events.put((json.dumps(event),processed))
        await asyncio.wait_for(processed,2)

    async def send(self,event):pass

    async def close(self):self.closed=True


def test_rejected_item_cannot_release_accepted_item_drain(tmp_path,monkeypatch):
    settings=Settings(database_url='sqlite:///'+str(tmp_path/'sideband.db'),provider_mode='fixture',run_inline_worker=False,
        pricing_json={'version':'2026-10-25-test-rates','effective_date':'2026-10-25','currency':'USD',
            'text_input_per_million':'2','text_output_per_million':'8','audio_per_minute':'0.10'})
    database=Database(settings.database_url,settings)
    database.initialize();seed(database)
    payload={'attempt_id':'attempt-demo-success','input_mode':'voice'}
    session=service.mutate(database,'learner-1','/viva/sessions',str(uuid4()),payload,
        lambda db:service.start(db,'learner-1',payload,settings))
    sid=session['id']
    with database.session_factory.begin() as db:
        row=db.get(VivaSession,sid);row.status='connecting';row.connection_epoch=1
        db.add(VoiceConnection(id=uid(),session_id=sid,epoch=1,status='connecting'))
    service.voice_ready(database,sid,'learner-1',1,'fake-call')
    service.mark_question_delivered(database,sid,'learner-1',1)
    monkeypatch.setattr(budget,'settle',lambda *args:None)

    async def exercise():
        socket=Socket()
        task=asyncio.create_task(voice._sideband(socket,database,sid,'learner-1',1,None,asyncio.Event()))
        drain=None
        try:
            await socket.emit({'type':'input_audio_buffer.speech_started','item_id':'A'})
            pending_done=voice._PENDING[sid]
            assert not pending_done.is_set()
            await socket.emit({'type':'input_audio_buffer.speech_started','item_id':'B'})
            await socket.emit({'type':'conversation.item.input_audio_transcription.completed','item_id':'B','transcript':'Rejected overlap.'})
            assert not pending_done.is_set()
            assert service.pending_voice_input(database,sid,'learner-1')['id']=='A'
            drain=asyncio.create_task(voice.drain_pending(database,sid,'learner-1'))
            # The durable drain start is also the barrier before A finalizes.
            async with asyncio.timeout(2):
                while sid not in voice._DRAINING:await asyncio.sleep(0)
            assert not drain.done()
            await socket.emit({'type':'conversation.item.input_audio_transcription.completed','item_id':'A','transcript':'My observations support the check.'})
            assert await asyncio.wait_for(drain,2) is True
            assert pending_done.is_set()
            with database.session_factory() as db:
                turns=[turn for turn in service.turns_for(db,sid) if turn['speaker']=='learner']
                assert [turn['text'] for turn in turns]==['My observations support the check.']
                connection=db.scalar(select(VoiceConnection).where(VoiceConnection.session_id==sid))
                assert connection.pending_utterance_id is None
                assert connection.pending_question_id is None
        finally:
            task.cancel()
            await task
            if drain is not None and not drain.done():
                drain.cancel()
                await asyncio.gather(drain,return_exceptions=True)
        assert socket.closed
        assert sid not in voice._PENDING and sid not in voice._DRAINING

    try:asyncio.run(exercise())
    finally:database.engine.dispose()
