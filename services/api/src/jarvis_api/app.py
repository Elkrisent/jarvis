"""Control-plane api app: liveness, readiness and migrations at startup (M0.3, M1.1)."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from jarvis_core.health import (
    Dependency,
    Probe,
    dependency_from_url,
    evaluate_readiness,
    tcp_reachable,
)

from jarvis_api.db.migrate import run_migrations


def dependencies_from_env(env: dict[str, str] | None = None) -> list[Dependency]:
    env = dict(os.environ) if env is None else env
    # Postgres is required; sessions fall back to Postgres, so Redis is optional (SPEC §11.1).
    deps = [
        dependency_from_url("postgres", env["DATABASE_URL"]),
        dependency_from_url("redis", env["REDIS_URL"], required=False),
    ]
    return deps


def create_app(
    dependencies: Sequence[Dependency] = (),
    probe: Probe = tcp_reachable,
    migrate: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # A failed migration raises here, so the app never serves against a stale schema.
        if migrate is not None:
            await migrate()
        yield

    app = FastAPI(title="jarvis-api", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, object]:
        readiness = await evaluate_readiness(dependencies, probe)
        response.status_code = readiness.http_status
        return readiness.to_body()

    return app


def _configure_logging() -> None:
    """Show this package's INFO logs; uvicorn only sets up handlers for its own loggers."""
    logger = logging.getLogger("jarvis_api")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        logger.addHandler(handler)


def create_app_from_env() -> FastAPI:
    _configure_logging()
    env = dict(os.environ)
    database_url = env.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set (see deploy/.env.example)")

    async def migrate() -> None:
        await run_migrations(database_url)

    return create_app(dependencies_from_env(env), migrate=migrate)
