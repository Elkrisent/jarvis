"""ConfigManager against a real Postgres and Redis, driven through the real management API.

The management app is built from ``jarvis_api`` (test-only import). Propagation assertions poll the
store every 10 ms and fail when the change took longer than the budget to show up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from jarvis_api.app import create_app
from jarvis_api.db.config_source import PostgresConfigSource
from jarvis_api.db.migrate import to_async_url
from jarvis_api.db.repositories import KeyRepository, ProjectRepository
from jarvis_api.db.secrets import PostgresSecretStore
from jarvis_api.events import RedisConfigPublisher
from jarvis_api.http.deps import Services
from jarvis_core.clock import SystemClock
from jarvis_core.config import ApiDependency
from jarvis_core.keys import hash_jarvis_key
from jarvis_core.secretbox import AesGcmSecretBox
from jarvis_gateway.config.manager import ConfigManager
from jarvis_gateway.config.postgres import ConfigLoadError, PostgresLoader
from jarvis_gateway.config.snapshot import Snapshot
from jarvis_gateway.config.snapshot_file import CoalescingWriter, SnapshotFile
from jarvis_gateway.config.store import ConfigStore
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.integration

ADMIN_TOKEN = "gateway-it-admin-token-" + "x" * 20
CLOSED_REDIS = "redis://127.0.0.1:1/0"
BUDGET_S = 1.0
POLL_S = 0.01


def _redis_url() -> str:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.fail("REDIS_URL must point at a Redis server to run integration tests")
    return url


async def _within(condition: Callable[[], bool], budget_s: float = BUDGET_S) -> float:
    """Seconds until ``condition`` held; fails if that took longer than ``budget_s``."""
    start = time.monotonic()
    deadline = start + budget_s + 2.0  # keep polling a little past the budget, for the message
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"condition not met within {budget_s + 2.0:.1f} s")
        await asyncio.sleep(POLL_S)
    elapsed = time.monotonic() - start
    assert elapsed <= budget_s, f"took {elapsed:.3f} s, budget {budget_s} s"
    return elapsed


@dataclass
class Gateway:
    manager: ConfigManager
    store: ConfigStore
    file: SnapshotFile

    @property
    def snap(self) -> Snapshot:
        current = self.store.current
        assert current is not None
        return current


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(to_async_url(database_url))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def api(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """The real management API, publishing to the real Redis."""
    publisher = RedisConfigPublisher(_redis_url())
    services = Services(
        projects=ProjectRepository(engine),
        keys=KeyRepository(engine),
        apis=PostgresConfigSource(engine),
        secrets=PostgresSecretStore(engine),
        secret_box=AesGcmSecretBox(bytes(range(32))),
        publisher=publisher,
    )
    app = create_app(services=services, admin_token=ADMIN_TOKEN)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    try:
        yield client
    finally:
        await client.aclose()
        await publisher.aclose()


GatewayFactory = Callable[..., Any]


@pytest.fixture
async def gateway(database_url: str, tmp_path: Path) -> AsyncIterator[GatewayFactory]:
    started: list[ConfigManager] = []

    async def make(
        *,
        redis_url: str | None,
        resync_interval_s: float = 60.0,
        loader: PostgresLoader | None = None,
    ) -> Gateway:
        clock = SystemClock()
        file = SnapshotFile(tmp_path / "snapshot.json")
        store = ConfigStore()
        manager = ConfigManager(
            loader or PostgresLoader(database_url, connect_timeout_s=2.0),
            file,
            CoalescingWriter(file, clock),
            store,
            clock,
            resync_interval_s=resync_interval_s,
            redis_url=redis_url,
        )
        await manager.start()
        started.append(manager)
        return Gateway(manager, store, file)

    try:
        yield make
    finally:
        for manager in started:
            await manager.stop()


async def _project(api: httpx.AsyncClient, slug: str) -> str:
    response = await api.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert response.status_code == 201, response.text
    pid: str = response.json()["id"]
    return pid


async def _create_api(api: httpx.AsyncClient, pid: str, slug: str) -> str:
    body = {"slug": slug, "name": slug.title(), "base_url": f"https://{slug}.example.com"}
    response = await api.post(f"/api/v1/projects/{pid}/apis", json=body)
    assert response.status_code == 201, response.text
    api_id: str = response.json()["id"]
    return api_id


async def _subscribed(api: httpx.AsyncClient, gw: Gateway) -> tuple[str, str]:
    """A project with one API, seen by the gateway: proof that its subscription is live.

    The startup load ran before the API existed, so only the subscriber (an event, or the resync
    it runs once subscribed) can have brought it in.
    """
    pid = await _project(api, "warmup")
    api_id = await _create_api(api, pid, "warmup")
    await _within(lambda: gw.snap.api_by_id(api_id) is not None, budget_s=10.0)
    return pid, api_id


def _version(gw: Gateway, api_id: str) -> int | None:
    entry = gw.snap.api_by_id(api_id)
    return None if entry is None else entry.version


@pytest.mark.req("REQ-5.2-3")
async def test_an_api_edit_reaches_the_store_within_1s(
    api: httpx.AsyncClient, gateway: GatewayFactory
) -> None:
    gw: Gateway = await gateway(redis_url=_redis_url())
    pid, api_id = await _subscribed(api, gw)

    for deadline in (6000, 7000, 8000):
        response = await api.patch(f"/api/v1/apis/{api_id}", json={"deadline_ms": deadline})
        assert response.status_code == 200, response.text
        version: int = response.json()["version"]

        def caught_up(expected: int = version) -> bool:
            return _version(gw, api_id) == expected

        await _within(caught_up)
        entry = gw.snap.api_by_id(api_id)
        assert entry is not None
        assert entry.config.deadline_ms == deadline

    created = await _create_api(api, pid, "maps")
    await _within(lambda: gw.snap.api(pid, "maps") is not None)
    assert gw.snap.api_by_id(created) is not None


@pytest.mark.req("REQ-4.2-6", "REQ-11.1-6")
async def test_key_create_and_revoke_reach_the_store_within_1s(
    api: httpx.AsyncClient, gateway: GatewayFactory
) -> None:
    gw: Gateway = await gateway(redis_url=_redis_url())
    pid, api_id = await _subscribed(api, gw)

    response = await api.post(
        f"/api/v1/projects/{pid}/keys", json={"name": "ci", "allowed_api_ids": [api_id]}
    )
    assert response.status_code == 201, response.text
    key_hash = hash_jarvis_key(response.json()["key"])
    key_id = response.json()["id"]
    await _within(lambda: gw.snap.key(key_hash) is not None)
    entry = gw.snap.key(key_hash)
    assert entry is not None
    assert (entry.key_id, entry.revoked, entry.allowed_api_ids) == (
        key_id,
        False,
        frozenset({api_id}),
    )

    revoked = await api.delete(f"/api/v1/keys/{key_id}")
    assert revoked.status_code == 204

    def is_revoked() -> bool:
        key = gw.snap.key(key_hash)
        return key is not None and key.revoked

    await _within(is_revoked)


@pytest.mark.req("REQ-5.2-3")
async def test_api_and_project_deletes_remove_entries_within_1s(
    api: httpx.AsyncClient, gateway: GatewayFactory
) -> None:
    gw: Gateway = await gateway(redis_url=_redis_url())
    warm_pid, warm_api = await _subscribed(api, gw)
    pid = await _project(api, "doomed")
    weather = await _create_api(api, pid, "weather")
    maps = await _create_api(api, pid, "maps")
    key = await api.post(f"/api/v1/projects/{pid}/keys", json={"name": "k"})
    key_hash = hash_jarvis_key(key.json()["key"])
    await _within(
        lambda: all(gw.snap.api_by_id(a) for a in (weather, maps)) and bool(gw.snap.key(key_hash))
    )

    assert (await api.delete(f"/api/v1/apis/{weather}")).status_code == 204
    await _within(lambda: gw.snap.api_by_id(weather) is None)
    assert gw.snap.api_by_id(maps) is not None

    assert (await api.delete(f"/api/v1/projects/{pid}")).status_code == 204
    await _within(lambda: gw.snap.api_by_id(maps) is None and gw.snap.key(key_hash) is None)
    assert gw.snap.api_by_id(warm_api) is not None  # other projects untouched
    assert gw.snap.api(warm_pid, "warmup") is not None


@pytest.mark.req("REQ-5.2-4")
async def test_a_missed_message_is_corrected_by_the_resync(
    engine: AsyncEngine, gateway: GatewayFactory
) -> None:
    pid = (await ProjectRepository(engine).create("Quiet", "quiet")).id
    source = PostgresConfigSource(engine)
    api_id, _ = await source.put_version(pid, _dependency("weather", 6000))
    gw: Gateway = await gateway(redis_url=None, resync_interval_s=1.0)  # no subscriber at all
    assert _version(gw, api_id) == 1

    await source.put_version(pid, _dependency("weather", 7000), api_id=api_id)  # nothing published
    await _within(lambda: _version(gw, api_id) == 2, budget_s=2.0)


@pytest.mark.req("REQ-8-2", "REQ-5.2-4")
async def test_redis_unreachable_startup_works_and_the_resync_converges(
    engine: AsyncEngine, gateway: GatewayFactory
) -> None:
    pid = (await ProjectRepository(engine).create("NoRedis", "no-redis")).id
    source = PostgresConfigSource(engine)
    api_id, _ = await source.put_version(pid, _dependency("weather", 6000))
    gw: Gateway = await gateway(redis_url=CLOSED_REDIS, resync_interval_s=1.0)
    assert gw.manager.status().source == "postgres"
    assert _version(gw, api_id) == 1

    await source.put_version(pid, _dependency("weather", 7000), api_id=api_id)
    await _within(lambda: _version(gw, api_id) == 2, budget_s=2.0)


@pytest.mark.req("REQ-8-2")
async def test_postgres_outage_after_startup_serves_the_last_snapshot_and_recovers(
    engine: AsyncEngine,
    database_url: str,
    gateway: GatewayFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pid = (await ProjectRepository(engine).create("Outage", "outage")).id
    source = PostgresConfigSource(engine)
    api_id, _ = await source.put_version(pid, _dependency("weather", 6000))
    loader = CountingLoader(database_url, connect_timeout_s=2.0)
    gw: Gateway = await gateway(redis_url=None, resync_interval_s=1.0, loader=loader)

    url = make_url(database_url)
    name = url.database
    assert name is not None
    admin_dsn = url.set(drivername="postgresql", database="postgres")
    admin = await asyncpg.connect(admin_dsn.render_as_string(hide_password=False))
    try:
        with caplog.at_level(logging.INFO, logger="jarvis_gateway.config.manager"):
            await _block_connections(admin, name, engine)
            # Once one resync has failed, no later one can succeed: capture the state then.
            await _within(lambda: loader.failures >= 1, budget_s=5.0)
            before = gw.snap
            first_ok = gw.manager.status().last_sync_ok
            assert first_ok is not None
            seen = loader.failures
            await _within(lambda: loader.failures >= seen + 2, budget_s=5.0)
        failures = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(failures) == 1  # once per failure streak
        assert "configuration resync failed" in failures[0].getMessage()
        assert "jarvis-test" not in caplog.text
        assert "127.0.0.1" not in caplog.text
        assert gw.store.current is before  # still serving the last snapshot
        status = gw.manager.status()
        assert status.source == "postgres"
        assert status.last_sync_ok == first_ok
        assert time.time() - first_ok >= 1.0  # two failed 1 s resyncs later: it ages

        await admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
    finally:
        await admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
        await admin.close()
    await source.put_version(pid, _dependency("weather", 8000), api_id=api_id)

    def recovered() -> bool:
        ok = gw.manager.status().last_sync_ok
        return ok is not None and ok > first_ok and _version(gw, api_id) == 2

    await _within(recovered, budget_s=3.0)


class CountingLoader(PostgresLoader):
    """The real loader, counting failed full loads."""

    def __init__(self, dsn: str, *, connect_timeout_s: float) -> None:
        super().__init__(dsn, connect_timeout_s=connect_timeout_s)
        self.failures = 0

    async def load_all(self, now: float) -> tuple[Snapshot, frozenset[str]]:
        try:
            return await super().load_all(now)
        except ConfigLoadError:
            self.failures += 1
            raise


async def _block_connections(admin: asyncpg.Connection, name: str, engine: AsyncEngine) -> None:
    await engine.dispose()
    await admin.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false')
    await admin.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
        " WHERE datname = $1 AND pid <> pg_backend_pid()",
        name,
    )


def _dependency(slug: str, deadline_ms: int) -> ApiDependency:
    return ApiDependency.model_validate(
        {
            "slug": slug,
            "name": slug.title(),
            "base_url": f"https://{slug}.example.com",
            "deadline_ms": deadline_ms,
        }
    )
