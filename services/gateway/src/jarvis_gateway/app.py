"""Minimal gateway app: liveness and readiness only (M0.3)."""

from __future__ import annotations

import gc
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
from jarvis_core.interfaces import TelemetrySink

from jarvis_gateway.config.manager import ConfigManager, ConfigStatus
from jarvis_gateway.config.postgres import PostgresLoader
from jarvis_gateway.config.snapshot_file import CoalescingWriter, SnapshotFile
from jarvis_gateway.config.store import ConfigStore
from jarvis_gateway.credentials import CredentialVault
from jarvis_gateway.forwarder import Forwarder
from jarvis_gateway.routes import build_gw_router
from jarvis_gateway.routing import TemplateRegistry
from jarvis_gateway.settings import load_settings
from jarvis_gateway.telemetry import (
    ManagedSink,
    NoopTelemetrySink,
    RedisStreamWriter,
    RedisTelemetrySink,
)

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
    forwarder: Forwarder | None = None,
    sink: TelemetrySink | None = None,
    freeze_gc: bool = False,
) -> FastAPI:
    """Without ``config`` the app only reports its dependencies, exactly as in M0.3.

    ``freeze_gc`` moves everything alive after startup (modules, framework and config objects) out
    of the garbage collector's reach, so a full collection under load no longer walks them: those
    pauses were 20 to 70 ms every 30 s at 100 req/s. The gateway process turns it on.
    """
    clock = SystemClock() if clock is None else clock

    managed_sink = sink if isinstance(sink, ManagedSink) else None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if managed_sink is not None:
            await managed_sink.start()
        try:
            try:
                if config is None:
                    yield
                    return
                try:
                    await config.start()
                    if freeze_gc:
                        gc.collect()
                        gc.freeze()
                    yield
                finally:
                    await config.stop()
            finally:
                if forwarder is not None:
                    await forwarder.aclose()
        finally:
            if managed_sink is not None:  # last: requests that were still finishing are flushed
                await managed_sink.aclose()

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
        if managed_sink is not None:  # numbers only; telemetry loss never changes the status
            body["telemetry"] = {
                "queue": managed_sink.queue_depth,
                "dropped_total": managed_sink.dropped_total,
            }
        response.status_code = 503 if body["status"] == "unavailable" else 200
        return body

    if config is not None:
        templates = TemplateRegistry()
        config.store.add_listener(templates.retain)
        app.include_router(build_gw_router(config.store, forwarder, sink, templates, clock))

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
        snapshot_max_age_s=settings.snapshot_max_age_s,
    )
    vault = CredentialVault(settings.secret_box)
    forwarder = Forwarder(settings.ssrf_policy, vault, max_body_bytes=settings.max_body_bytes)
    store.add_listener(vault.refresh)  # secrets are opened before the forwarder can see the config
    store.add_listener(forwarder.retain)
    sink: TelemetrySink = (
        RedisTelemetrySink(
            RedisStreamWriter(settings.redis_url),
            queue_size=settings.telemetry_queue_size,
            batch_size=settings.telemetry_batch_size,
            maxlen=settings.telemetry_maxlen,
            clock=clock,
        )
        if settings.telemetry_enabled
        else NoopTelemetrySink()
    )
    return create_app(
        dependencies_from_env(),
        config=manager,
        resync_interval_s=settings.resync_interval_s,
        clock=clock,
        forwarder=forwarder,
        sink=sink,
        freeze_gc=True,
    )
