"""Config propagation benchmark (metric ``config_propagation_p99``, SPEC §13: <= 1 s).

Drives the real management API (PATCH an API, 200 times, ~20 ms apart) against a real Postgres and
Redis and a real ``ConfigManager``, and measures how long the gateway's store takes to show each
new version.

Each sample starts just BEFORE the PATCH is sent and ends when the store shows the new version, so
it includes the HTTP request itself. Starting after the response would under-measure the
commit-to-store latency (the event can reach the gateway before the response is read). Measuring
from request start is therefore a conservative upper bound on the propagation time.

The p99 merges into ``$BENCH_DIR/results.json`` (default ``build/bench``); other metrics there
are kept. ``propagation.json`` beside it feeds the trend chart. Run with ``make bench-propagation``.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from benchlib import judge, load_budgets, read_json, write_json
from jarvis_api.app import create_app
from jarvis_api.db.config_source import PostgresConfigSource
from jarvis_api.db.migrate import to_async_url
from jarvis_api.db.repositories import KeyRepository, ProjectRepository
from jarvis_api.db.secrets import PostgresSecretStore
from jarvis_api.events import RedisConfigPublisher
from jarvis_api.http.deps import Services
from jarvis_core.clock import SystemClock
from jarvis_core.secretbox import AesGcmSecretBox
from jarvis_gateway.config.manager import ConfigManager
from jarvis_gateway.config.postgres import PostgresLoader
from jarvis_gateway.config.snapshot_file import CoalescingWriter, SnapshotFile
from jarvis_gateway.config.store import ConfigStore
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.perf

ROOT = Path(__file__).resolve().parents[4]
METRIC = "config_propagation_p99"
SAMPLES = 200
SPACING_S = 0.02
POLL_S = 0.001
TIMEOUT_S = 5.0
ADMIN_TOKEN = "gateway-bench-admin-token-" + "x" * 20


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(to_async_url(database_url))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.req("REQ-13-5", "REQ-5.2-3")
async def test_config_propagation_p99_is_within_budget(
    engine: AsyncEngine, database_url: str, tmp_path: Path
) -> None:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        pytest.fail("REDIS_URL must point at a Redis server to run this benchmark")
    budget = load_budgets(ROOT / "benchmarks" / "budgets.yaml")[METRIC]
    assert budget.budget is not None

    publisher = RedisConfigPublisher(redis_url)
    services = Services(
        projects=ProjectRepository(engine),
        keys=KeyRepository(engine),
        apis=PostgresConfigSource(engine),
        secrets=PostgresSecretStore(engine),
        secret_box=AesGcmSecretBox(bytes(range(32))),
        publisher=publisher,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services=services, admin_token=ADMIN_TOKEN)),
        base_url="http://test",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    clock = SystemClock()
    file = SnapshotFile(tmp_path / "snapshot.json")
    store = ConfigStore()
    manager = ConfigManager(
        PostgresLoader(database_url, connect_timeout_s=2.0),
        file,
        CoalescingWriter(file, clock),
        store,
        clock,
        resync_interval_s=60.0,
        redis_url=redis_url,
    )
    await manager.start()
    try:
        project = await client.post("/api/v1/projects", json={"name": "bench", "slug": "bench"})
        assert project.status_code == 201, project.text
        pid = project.json()["id"]
        created = await client.post(
            f"/api/v1/projects/{pid}/apis",
            json={"slug": "weather", "name": "Weather", "base_url": "https://weather.example.com"},
        )
        assert created.status_code == 201, created.text
        api_id: str = created.json()["id"]

        def seen_version() -> int | None:
            snap = store.current
            entry = None if snap is None else snap.api_by_id(api_id)
            return None if entry is None else entry.version

        # The startup load ran before the API existed: wait until the subscription delivered it.
        started = time.monotonic()
        while seen_version() is None:
            assert time.monotonic() - started < 10.0, "the gateway never saw the new API"
            await asyncio.sleep(POLL_S)

        samples_ms: list[float] = []
        for i in range(SAMPLES):
            begin = time.perf_counter()  # before the request is sent: includes the HTTP time
            response = await client.patch(f"/api/v1/apis/{api_id}", json={"deadline_ms": 5000 + i})
            assert response.status_code == 200, response.text
            version: int = response.json()["version"]
            while (seen_version() or 0) < version:
                assert time.perf_counter() - begin < TIMEOUT_S, f"version {version} never arrived"
                await asyncio.sleep(POLL_S)
            samples_ms.append((time.perf_counter() - begin) * 1000)
            await asyncio.sleep(SPACING_S)
    finally:
        await manager.stop()
        await client.aclose()
        await publisher.aclose()

    p50 = statistics.median(samples_ms)
    p99 = _percentile(samples_ms, 99)
    print(f"\nconfig propagation over {SAMPLES} PATCHes: p50={p50:.2f} ms p99={p99:.2f} ms")

    bench_dir = Path(os.environ.get("BENCH_DIR", "build/bench"))
    # github-action-benchmark input (customSmallerIsBetter) for the trend chart.
    write_json(
        bench_dir / "propagation.json",
        [{"name": METRIC, "unit": budget.unit, "value": round(p99, 3)}],
    )
    results_path = bench_dir / "results.json"
    results = read_json(results_path) if results_path.exists() else {}
    results[METRIC] = round(p99, 3)
    write_json(results_path, results)

    assert judge(p99, budget) == "ok", f"p99 {p99:.1f} ms over budget {budget.budget:g} ms"
