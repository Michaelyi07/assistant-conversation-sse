"""Shared fixtures: ephemeral PostgreSQL 16 test database migrated via Alembic
(no create_all), app under a real uvicorn server, SSE parsing helpers."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from pathlib import Path

import psycopg
import pytest
import uvicorn

# psycopg async mode requires a selector event loop on Windows (Proactor default).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

REPO_ROOT = Path(__file__).resolve().parents[1]

PG_ADMIN_URL = "postgresql://postgres@127.0.0.1:5433/postgres"


@pytest.fixture(scope="session")
def test_db_url() -> str:
    """Create an ephemeral test database, migrate it to head with Alembic, drop it after."""
    dbname = f"test_asst_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(PG_ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    url = f"postgresql+psycopg://postgres@127.0.0.1:5433/{dbname}"
    import os

    os.environ["DATABASE_URL"] = url
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    yield url
    # Drop after all sessions are closed.
    with psycopg.connect(PG_ADMIN_URL, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (dbname,),
        )
        conn.execute(f'DROP DATABASE "{dbname}"')


@pytest.fixture()
def app(test_db_url):
    from app.config import Settings
    from app.main import create_app

    settings = Settings()  # picks up DATABASE_URL from env set by test_db_url
    application = create_app(settings=settings)
    return application


@pytest.fixture()
def server(app):
    """Run the app on a real uvicorn server (ephemeral port) in a background thread."""
    # loop="none": let the process-level event loop policy apply (selector on
    # Windows; uvicorn 0.54's own factory forces ProactorEventLoop on win32,
    # which psycopg async cannot use).
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", loop="none")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        if time.monotonic() > deadline or srv.should_exit:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture()
async def clean_state(app):
    """Reset DB tables and in-memory stubs between tests."""
    import sqlalchemy

    from app.db import make_engine

    engine = make_engine(app.state.settings.database_url)
    async with engine.connect() as conn:
        await conn.execute(sqlalchemy.text("TRUNCATE messages, conversations CASCADE"))
        await conn.commit()
    await engine.dispose()
    app.state.review_queue.entries.clear()
    yield app


def rewire(app, gateway=None, executor=None, review_queue=None, settings_overrides=None):
    """Swap injected components and rebuild the orchestrator (tests call this)."""
    from app.orchestration.pipeline import Orchestrator

    settings = app.state.settings
    if settings_overrides:
        for k, v in settings_overrides.items():
            setattr(settings, k, v)
    if gateway is not None:
        app.state.gateway = gateway
    if executor is not None:
        app.state.executor = executor
    if review_queue is not None:
        app.state.review_queue = review_queue
    app.state.orchestrator = Orchestrator(
        settings=settings,
        gateway=app.state.gateway,
        executor=app.state.executor,
        review_queue=app.state.review_queue,
        limiter=app.state.limiter,
        cancel_registry=app.state.cancel_registry,
        session_factory=app.state.session_factory,
    )


def parse_sse(raw: bytes | str) -> list[tuple[str, dict]]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    events = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        ev_name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev_name = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        if ev_name is not None:
            events.append((ev_name, json.loads(data) if data else {}))
    return events


def sync_db(url: str) -> psycopg.Connection:
    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1))
