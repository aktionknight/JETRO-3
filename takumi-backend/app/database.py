from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import Request
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase

SCHEMA_VERSION = 8


class Base(DeclarativeBase):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def build_engine(settings):
    url = make_url(settings.database_url)
    kwargs = {}
    if url.get_backend_name() == "sqlite":
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def sqlite_pragmas(connection, _):
            # Let SQLAlchemy emit BEGIN itself so SAVEPOINTs work (pysqlite legacy mode does not).
            connection.isolation_level = None
            cursor = connection.cursor()
            for pragma in ("foreign_keys=ON", "busy_timeout=5000", "journal_mode=WAL"):
                cursor.execute(f"PRAGMA {pragma}")
            cursor.close()

        @event.listens_for(engine, "begin")
        def sqlite_begin(connection):
            # IMMEDIATE serializes writers up front; a deferred BEGIN lets a stale WAL read
            # snapshot fail on write with SQLITE_BUSY_SNAPSHOT instead of REVISION_CONFLICT.
            connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def init_db(engine):
    import app.models.knowledge_ai  # noqa: F401  (register table before create_all)
    from app.models.entities import AppMeta

    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS releases_immutable BEFORE UPDATE ON releases "
                "BEGIN SELECT RAISE(ABORT, 'releases are immutable'); END;"
            )
        )
        version = connection.execute(
            text("SELECT value FROM app_meta WHERE key='schema_version'")
        ).scalar_one_or_none()
        if version is None:
            connection.execute(
                AppMeta.__table__.insert().values(key="schema_version", value=str(SCHEMA_VERSION))
            )
        elif version.isdecimal() and 1 <= int(version) < SCHEMA_VERSION:
            # every upgrade since v1 only adds tables, which create_all already created
            connection.execute(
                text("UPDATE app_meta SET value=:version WHERE key='schema_version'"),
                {"version": str(SCHEMA_VERSION)},
            )
        elif version != str(SCHEMA_VERSION):
            raise RuntimeError(
                f"Database schema version {version} != expected {SCHEMA_VERSION}; "
                "back up and delete the database file, then restart"
            )


def get_db(request: Request):
    with request.app.state.sessionmaker() as session:
        yield session
