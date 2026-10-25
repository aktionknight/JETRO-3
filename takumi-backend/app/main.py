import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import sessionmaker

from app.api import (
    auth,
    clarifications,
    demo,
    drafts,
    evidence,
    health,
    media,
    qa,
    releases,
    studio,
    studio_analysis,
    tasks,
)
from app.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.logging import add_http_middlewares, configure_logging
from app.database import build_engine, init_db
from app.learning import live_interview
from app.learning import router as learning
from app.services.seed_service import seed_demo


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    engine = build_engine(settings)

    @asynccontextmanager
    async def lifespan(app):
        init_db(engine)
        from app.services.studio_analysis_service import recover_interrupted

        with app.state.sessionmaker() as session, session.begin():
            recover_interrupted(session)
        if settings.demo_mode and settings.enable_demo_seed:
            with app.state.sessionmaker() as session, session.begin():
                seed_demo(session, settings.demo_seed_state)
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker(engine, expire_on_commit=False)
    secret = settings.demo_signing_secret
    app.state.signing_key = secret.get_secret_value() if secret else secrets.token_urlsafe(32)
    app.dependency_overrides[get_settings] = lambda: app.state.settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Range"],
        expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
    )
    add_http_middlewares(app, settings)
    register_error_handlers(app)
    for module in (
        health,
        auth,
        tasks,
        drafts,
        clarifications,
        releases,
        evidence,
        qa,
        media,
        demo,
        learning,
        live_interview,
        studio,
        studio_analysis,
    ):
        app.include_router(module.router, prefix="/api/v1")
    return app


app = create_app()
