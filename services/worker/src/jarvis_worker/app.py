"""Minimal worker app: liveness and readiness only (M0.3)."""

from __future__ import annotations

import os
from collections.abc import Sequence

from fastapi import FastAPI, Response
from jarvis_core.health import (
    Dependency,
    Probe,
    dependency_from_url,
    evaluate_readiness,
    tcp_reachable,
)


def dependencies_from_env(env: dict[str, str] | None = None) -> list[Dependency]:
    env = dict(os.environ) if env is None else env
    # The worker consumes the Redis stream and writes to Postgres: it needs both.
    deps = [
        dependency_from_url("postgres", env["DATABASE_URL"]),
        dependency_from_url("redis", env["REDIS_URL"]),
    ]
    return deps


def create_app(dependencies: Sequence[Dependency] = (), probe: Probe = tcp_reachable) -> FastAPI:
    app = FastAPI(title="jarvis-worker")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, object]:
        readiness = await evaluate_readiness(dependencies, probe)
        response.status_code = readiness.http_status
        return readiness.to_body()

    return app


def create_app_from_env() -> FastAPI:
    return create_app(dependencies_from_env())
