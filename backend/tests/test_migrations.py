"""Ownership migration preserves populated legacy data in both directions."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, inspect, select


def test_voice_question_ownership_upgrade_and_downgrade(tmp_path,monkeypatch):
    root=Path(__file__).resolve().parents[1]
    url='sqlite:///'+str(tmp_path/'migration.db')
    monkeypatch.setenv('VIVA_DATABASE_URL',url)
    config=Config(str(root/'alembic.ini'))
    config.set_main_option('script_location',str(root/'alembic'))
    command.upgrade(config,'0003_pending_voice')
    engine=create_engine(url)
    instant=datetime.now(timezone.utc)
    metadata=MetaData();metadata.reflect(engine)
    with engine.begin() as db:
        db.execute(metadata.tables['releases'].insert().values(id='release',task_id='task',title='Kept release',content_hash='hash',data={'source':'retained'}))
        db.execute(metadata.tables['playground_attempts'].insert().values(id='attempt',learner_id='learner',task_id='task',status='completed',release_id='release',bundle_id='bundle',bundle_hash='hash',scenario_id='scenario',data={'evidence':'retained'},created_at=instant))
        db.execute(metadata.tables['viva_sessions'].insert().values(id='session',learner_id='learner',attempt_id='attempt',release_id='release',bundle_id='bundle',bundle_hash='hash',status='in_progress',input_mode='voice',revision=7,active_seconds=12,created_at=instant,expires_at=instant+timedelta(hours=1),current_index=0,current_question={'id':'question'},coverage=[{'question_id':'question','status':'unanswered'}],connection_epoch=1,event_sequence=0,rubric_version='rubric-v1',prompt_version='examiner-v1',model_config_version='fixture-v1'))
        db.execute(metadata.tables['viva_connections'].insert().values(id='connection',session_id='session',epoch=1,status='active',created_at=instant,pending_utterance_id='legacy-item',pending_since=instant,drain_started_at=instant))
        db.execute(metadata.tables['viva_turns'].insert().values(id='turn',session_id='session',sequence=1,question_id='question',speaker='interviewer',text='Kept question',input_mode='voice',delivery='completed',created_at=instant))
        db.execute(metadata.tables['viva_reports'].insert().values(id='report',session_id='session',snapshot_hash='snapshot',version=1,data={'result':'retained'},created_at=instant))
    names=('releases','playground_attempts','viva_sessions','viva_connections','viva_turns','viva_reports')

    def rows():
        schema=MetaData();schema.reflect(engine)
        with engine.connect() as db:
            return {name:[dict(row) for row in db.execute(select(schema.tables[name])).mappings()] for name in names}

    original=rows()
    old_columns={column['name'] for column in inspect(engine).get_columns('viva_connections')}
    try:
        command.upgrade(config,'head')
        upgraded=rows()
        assert upgraded['viva_connections'][0].pop('pending_question_id') is None
        assert upgraded==original
        columns={column['name'] for column in inspect(engine).get_columns('viva_connections')}
        assert columns==old_columns|{'pending_question_id'}
        with engine.begin() as db:
            schema=MetaData();schema.reflect(db)
            connection=schema.tables['viva_connections']
            db.execute(connection.update().where(connection.c.id=='connection').values(pending_question_id='question'))
        assert rows()['viva_connections'][0]['pending_question_id']=='question'
        command.downgrade(config,'0003_pending_voice')
        assert {column['name'] for column in inspect(engine).get_columns('viva_connections')}==old_columns
        assert rows()==original
        command.upgrade(config,'head')
        restored=rows()
        assert restored['viva_connections'][0].pop('pending_question_id') is None
        assert restored==original
    finally:engine.dispose()
