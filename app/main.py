"""FastAPI application factory for the assistant conversation orchestration interface."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.assistant import router
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.gateway.stub import DefaultStubGateway, ModelGateway
from app.orchestration.limiter import UserConcurrencyLimiter
from app.orchestration.pipeline import Orchestrator
from app.review.queue import ReviewQueueStub
from app.tools.executor import StubToolExecutor, ToolExecutor


def create_app(
    *,
    settings: Settings | None = None,
    gateway: ModelGateway | None = None,
    executor: ToolExecutor | None = None,
    review_queue: ReviewQueueStub | None = None,
) -> FastAPI:
    """Dependency injection points for tests and for connecting the real
    S5-GW gateway / S5-ASST-003 executor later."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        session_factory = make_session_factory(engine)
        app.state.session_factory = session_factory
        app.state.engine = engine
        app.state.orchestrator.session_factory = session_factory
        yield
        await engine.dispose()

    app = FastAPI(title="Assistant Conversation Orchestration API", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.gateway = gateway or DefaultStubGateway()
    app.state.executor = executor or StubToolExecutor()
    app.state.review_queue = review_queue or ReviewQueueStub(settings.sensitive_words)
    app.state.limiter = UserConcurrencyLimiter(settings.max_concurrent_generations)
    app.state.cancel_registry: dict[str, asyncio.Event] = {}
    app.state.orchestrator = Orchestrator(
        settings=settings,
        gateway=app.state.gateway,
        executor=app.state.executor,
        review_queue=app.state.review_queue,
        limiter=app.state.limiter,
        cancel_registry=app.state.cancel_registry,
        session_factory=None,  # wired in lifespan
    )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "assistant_disabled": settings.disabled}

    app.include_router(router)
    return app
