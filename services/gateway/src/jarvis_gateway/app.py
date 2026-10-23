"""Minimal gateway app: liveness and readiness only (M0.3)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Response
from jarvis_core.clock import Clock, SystemClock
from jarvis_core.health import (
    Dependency,
    Probe,
    dependency_from_url,
    evaluate_readiness,
    tcp_reachable,
)

from jarvis_gateway.config.manager import ConfigManager, ConfigStatus
from jarvis_gateway.config.postgres import PostgresLoader
from jarvis_gateway.config.snapshot_file import CoalescingWriter, SnapshotFile
from jarvis_gateway.config.store import ConfigStore
from jarvis_gateway.settings import load_settings

# A full sync older than this many resync intervals marks the gateway degraded.
STALE_AFTER_INTERVALS = 3


def dependencies_from_env(env: dict[str, str] | None = None) -> list[Dependency]:
    env = dict(os.environ) if env is None else env
    # Redis is optional: the gateway fails open without it (SPEC D3).
    deps = [dependency_from_url("redis", env["REDIS_URL"], required=False)]
    return deps


def _config_body(status: ConfigStatus) -> dict[str, object]:
    """Counts and times only: never slugs, hashes, secret references or policies."""
    return {
        "source": status.source,
        "apis": status.apis,
        "keys": status.keys,
        "synced_at": status.synced_at,
    }


def _config_state(
    status: ConfigStatus, now: float, resync_interval_s: float
) -> Literal["ok", "degraded", "unavailable"]:
    if status.source == "none":
        return "unavailable"
    if status.source == "file":
        return "degraded"
    stale_after = STALE_AFTER_INTERVALS * resync_interval_s
    if status.last_sync_ok is None or now - status.last_sync_ok > stale_after:
        return "degraded"
    return "ok"


def create_app(
    dependencies: Sequence[Dependency] = (),
    probe: Probe = tcp_reachable,
    *,
    config: ConfigManager | None = None,
    resync_interval_s: float = 60.0,
    clock: Clock | None = None,
) -> FastAPI:
    """Without ``config`` the app only reports its dependencies, exactly as in M0.3."""
    clock = SystemClock() if clock is None else clock

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if config is None:
            yield
            return
        try:
            await config.start()
            yield
        finally:
            await config.stop()

    app = FastAPI(title="jarvis-gateway", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, object]:
        readiness = await evaluate_readiness(dependencies, probe)
        body = readiness.to_body()
        if config is not None:
            status = config.status()
            body["config"] = _config_body(status)
            state = _config_state(status, clock.now(), resync_interval_s)
            if state == "unavailable" or readiness.status == "unavailable":
                body["status"] = "unavailable"
            elif state == "degraded" or readiness.status == "degraded":
                body["status"] = "degraded"
        response.status_code = 503 if body["status"] == "unavailable" else 200
        return body

    return app


def create_app_from_env() -> FastAPI:
    settings = load_settings()
    clock = SystemClock()
    store = ConfigStore()
    snapshot_file = SnapshotFile(settings.snapshot_path)
    manager = ConfigManager(
        PostgresLoader(settings.database_url),
        snapshot_file,
        CoalescingWriter(snapshot_file, clock),
        store,
        clock,
        resync_interval_s=settings.resync_interval_s,
        redis_url=settings.redis_url,
    )
    return create_app(
        dependencies_from_env(),
        config=manager,
        resync_interval_s=settings.resync_interval_s,
        clock=clock,
    )
