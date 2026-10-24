"""Telemetry on/off A/B (metric ``telemetry_overhead_p99``, M1.9 DoD: < 0.5 ms p99 difference).

The real gateway app is driven in process through ASGI against a stub upstream that answers after
1 ms. Three arms take turns in small blocks, so machine drift hits all of them equally:

* ``off``: the no-op sink;
* ``on``: ``RedisTelemetrySink`` writing to a real Redis;
* ``down``: the same sink pointed at a port nobody listens on (every batch fails and is dropped).

The flusher runs on the same event loop as the requests, so its work shows up in the numbers
exactly as it would in the gateway. The reported overhead is the median over several rounds of
``p99(arm) - p99(off)``; both ``on`` and ``down`` must stay under the budget. The cost of
``emit()`` itself is checked separately (< 20 us at p99). Run with ``make bench-telemetry``.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from benchlib import judge, load_budgets, read_json, write_json
from jarvis_core.config import ApiDependency
from jarvis_core.interfaces import TelemetrySink
from jarvis_core.keys import hash_jarvis_key
from jarvis_core.secretbox import AesGcmSecretBox
from jarvis_core.ssrf import SsrfPolicy
from jarvis_core.telemetry import event_from_outcome
from jarvis_gateway.app import create_app
from jarvis_gateway.config.manager import ConfigManager
from jarvis_gateway.config.snapshot import ApiEntry, KeyEntry, Snapshot
from jarvis_gateway.config.store import ConfigStore
from jarvis_gateway.credentials import CredentialVault
from jarvis_gateway.forwarder import Forwarder
from jarvis_gateway.telemetry import (
    NoopTelemetrySink,
    RedisStreamWriter,
    RedisTelemetrySink,
    StreamWriter,
)

pytestmark = pytest.mark.perf

ROOT = Path(__file__).resolve().parents[4]
METRIC = "telemetry_overhead_p99"
METRIC_DOWN = "telemetry_overhead_p99_redis_down"
KEY = "jv_live_benchtelemetry01"
ROUNDS = 9
BLOCKS_PER_ROUND = 8
BLOCK = 100  # requests per arm per block: 4,000 per arm in all
WARMUP = 300
UPSTREAM_MS = 0.001  # seconds: 1 ms
EMIT_CALLS = 50_000
EMIT_P99_BUDGET_US = 20.0
STREAM = "telemetry_bench"


class StoreOnly:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class NullWriter:
    async def xadd_batch(self, stream: str, maxlen: int, events: list[str]) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


class StubUpstream(httpx.AsyncBaseTransport):
    """Answers every request after a fixed delay, with a body that can be streamed."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(UPSTREAM_MS)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=httpx.ByteStream(b'{"ok":true}'),
        )


def _store() -> ConfigStore:
    store = ConfigStore()
    config = ApiDependency.model_validate(
        {"slug": "weather", "name": "Weather", "base_url": "https://bench.example.com"}
    )
    api = ApiEntry("api_1", "prj_1", "weather", 1, config)
    keys = {hash_jarvis_key(KEY): KeyEntry("key_1", "prj_1", frozenset(), False)}
    store.replace(Snapshot.empty("postgres", 1.0).with_changes(apis={"api_1": api}, keys=keys))
    return store


class Arm:
    def __init__(self, name: str, sink: TelemetrySink, store: ConfigStore) -> None:
        self.name = name
        self.sink = sink
        self.forwarder = Forwarder(
            SsrfPolicy(),
            CredentialVault(AesGcmSecretBox(bytes(range(32)))),
            max_body_bytes=1024 * 1024,
            transport_factory=StubUpstream,
        )
        app = create_app(
            config=cast(ConfigManager, StoreOnly(store)), forwarder=self.forwarder, sink=sink
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        )
        self.samples_ms: list[float] = []

    async def request(self) -> float:
        begin = time.perf_counter()
        response = await self.client.get("/gw/weather/v1/users/42", headers={"X-Jarvis-Key": KEY})
        elapsed = (time.perf_counter() - begin) * 1000
        assert response.status_code == 200
        return elapsed

    async def close(self) -> None:
        await self.client.aclose()
        await self.forwarder.aclose()
        aclose = getattr(self.sink, "aclose", None)
        if aclose is not None:
            await aclose()


@pytest.fixture
async def arms() -> AsyncIterator[dict[str, Arm]]:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        pytest.fail("REDIS_URL must point at a Redis server to run this benchmark")
    store = _store()
    sinks: dict[str, TelemetrySink] = {
        "off": NoopTelemetrySink(),
        "on": RedisTelemetrySink(RedisStreamWriter(redis_url), stream=STREAM),
        "down": RedisTelemetrySink(RedisStreamWriter("redis://127.0.0.1:1/0", timeout_s=0.2)),
    }
    built = {name: Arm(name, sink, store) for name, sink in sinks.items()}
    for sink in sinks.values():
        start = getattr(sink, "start", None)
        if start is not None:
            await start()
    try:
        yield built
    finally:
        for arm in built.values():
            await arm.close()
        import redis.asyncio as redis_asyncio

        client = redis_asyncio.from_url(redis_url)
        try:
            await client.delete(STREAM)
        finally:
            await client.aclose()


@pytest.mark.req("REQ-6.3-1", "REQ-6.3-4")
async def test_telemetry_adds_less_than_half_a_millisecond_at_p99(
    arms: dict[str, Arm],
) -> None:
    budgets = load_budgets(ROOT / "benchmarks" / "budgets.yaml")
    budget = budgets[METRIC]
    assert budget.budget is not None
    for arm in arms.values():  # warm-up: connections, caches, the JIT-free interpreter's paths
        for _ in range(WARMUP):
            await arm.request()

    diffs: dict[str, list[float]] = {"on": [], "down": []}
    p99s: dict[str, list[float]] = {name: [] for name in arms}
    for _ in range(ROUNDS):
        round_samples: dict[str, list[float]] = {name: [] for name in arms}
        for block in range(BLOCKS_PER_ROUND):
            order = list(arms.values())
            if block % 2:
                order.reverse()  # alternate who goes first
            for arm in order:
                for _ in range(BLOCK):
                    round_samples[arm.name].append(await arm.request())
        for name, samples in round_samples.items():
            p99s[name].append(_percentile(samples, 99))
        off = p99s["off"][-1]
        diffs["on"].append(p99s["on"][-1] - off)
        diffs["down"].append(p99s["down"][-1] - off)

    on = statistics.median(diffs["on"])
    down = statistics.median(diffs["down"])
    p50 = {n: statistics.median(s) for n, s in p99s.items()}
    print(
        "\ntelemetry A/B (median of per-round p99, ms): "
        + ", ".join(f"{n}={p50[n]:.3f}" for n in arms)
        + f"\n  overhead on={on:.3f} ms, redis down={down:.3f} ms; per-round on={diffs['on']}"
    )

    bench_dir = Path(os.environ.get("BENCH_DIR", "build/bench"))
    write_json(
        bench_dir / "telemetry.json",
        [
            {"name": METRIC, "unit": budget.unit, "value": round(on, 3)},
            {"name": METRIC_DOWN, "unit": budget.unit, "value": round(down, 3)},
        ],
    )
    results_path = bench_dir / "results.json"
    results = read_json(results_path) if results_path.exists() else {}
    results[METRIC] = round(on, 3)
    results[METRIC_DOWN] = round(down, 3)
    write_json(results_path, results)

    assert judge(on, budget) == "ok", (
        f"telemetry on adds {on:.3f} ms at p99 (budget {budget.budget:g})"
    )
    assert judge(down, budgets[METRIC_DOWN]) == "ok", f"Redis down adds {down:.3f} ms at p99"


@pytest.mark.req("REQ-6.3-4")
def test_emit_itself_is_cheap() -> None:
    sink = RedisTelemetrySink(cast(StreamWriter, NullWriter()), queue_size=EMIT_CALLS + 1)
    events = [
        event_from_outcome(
            request_id=f"r{n}",
            ts="2026-09-24T10:42:01.123Z",
            project_id="prj_1",
            key_id="key_1",
            api_id="api_1",
            method="GET",
            route="/v1/x",
            status=200,
            source="upstream",
            attempts=1,
            latency_ms=3,
            upstream_latency_ms=2,
            req_bytes=0,
            resp_bytes=10,
            error_code=None,
        )
        for n in range(EMIT_CALLS)
    ]
    samples_us: list[float] = []
    for event in events:
        begin = time.perf_counter()
        sink.emit(event)
        samples_us.append((time.perf_counter() - begin) * 1e6)
    p99 = _percentile(samples_us, 99)
    print(f"\nemit(): p50={statistics.median(samples_us):.2f} us p99={p99:.2f} us")
    assert sink.dropped_total == 0
    assert p99 < EMIT_P99_BUDGET_US, f"emit() p99 {p99:.1f} us"
