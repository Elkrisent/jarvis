"""Control-plane api app: liveness, readiness and migrations at startup (M0.3, M1.1)."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Response
from jarvis_core.health import (
    Dependency,
    Probe,
    dependency_from_url,
    evaluate_readiness,
    tcp_reachable,
)
from sqlalchemy.ext.asyncio import create_async_engine

from jarvis_api.db.config_source import PostgresConfigSource
from jarvis_api.db.migrate import run_migrations, to_async_url
from jarvis_api.db.repositories import KeyRepository, ProjectRepository
from jarvis_api.db.secrets import PostgresSecretStore
from jarvis_api.events import RedisConfigPublisher
from jarvis_api.http.deps import Services, require_admin
from jarvis_api.http.errors import install_error_handlers
from jarvis_api.http.router import router as api_router
from jarvis_api.settings import load_settings


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
    *,
    services: Services | None = None,
    admin_token: str | None = None,
    shutdown: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    if services is not None and not admin_token:
        raise ValueError("admin_token is required when services are given")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # A failed migration raises here, so the app never serves against a stale schema.
        if migrate is not None:
            await migrate()
        try:
            yield
        finally:
            if shutdown is not None:
                await shutdown()

    # Interactive docs are off and /openapi.json is served below, behind the admin token.
    app = FastAPI(
        title="jarvis-api", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    install_error_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, object]:
        readiness = await evaluate_readiness(dependencies, probe)
        response.status_code = readiness.http_status
        return readiness.to_body()

    if services is not None:
        app.state.services = services
        app.state.admin_token = admin_token
        app.include_router(api_router)

        @app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_admin)])
        async def openapi_schema() -> dict[str, object]:
            return app.openapi()

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
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is not set (see deploy/.env.example)")
    # Settings before any connection, so a missing or invalid key or token stops startup.
    settings = load_settings()
    database_url = settings.database_url
    engine = create_async_engine(to_async_url(database_url))
    publisher = RedisConfigPublisher(settings.redis_url)
    services = Services(
        projects=ProjectRepository(engine),
        keys=KeyRepository(engine),
        apis=PostgresConfigSource(engine),
        secrets=PostgresSecretStore(engine),
        secret_box=settings.secret_box,
        publisher=publisher,
    )

    async def migrate() -> None:
        await run_migrations(database_url)

    async def shutdown() -> None:
        await publisher.aclose()
        await engine.dispose()

    return create_app(
        dependencies_from_env(dict(os.environ)),
        migrate=migrate,
        services=services,
        admin_token=settings.admin_token,
        shutdown=shutdown,
    )
