"""M2.7: Redis killed and restored under load (SPEC D3, §8). Needs REDIS_URL.

The gateway's Redis helpers (rate limiter, circuit store, retry budget, health tracker) talk to
Redis through a small TCP proxy that this test can stop and restart: "killing" Redis closes every
connection and stops listening, "restoring" it listens again on the same port. Another mode makes
the proxy accept connections and never answer (a black-holed Redis).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import pytest
from jarvis_core.clock import SystemClock
from jarvis_core.memory import InMemoryCircuitStore, InMemoryRateLimiter, InMemoryTelemetrySink
from jarvis_gateway.circuit import CircuitBreaker
from jarvis_gateway.circuit_store import PREFIX as CIRCUIT_PREFIX
from jarvis_gateway.circuit_store import FallbackCircuitStore, RedisCircuitStore
from jarvis_gateway.rate_limit import PREFIX as LIMIT_PREFIX
from jarvis_gateway.rate_limit import FallbackRateLimiter, RedisRateLimiter
from jarvis_gateway.redis_health import RedisHealth
from jarvis_gateway.retry_budget import RedisBudgetBackend, RetryBudget
from redis import asyncio as redis_asyncio
from rig import GatewayFactory, api_entry
from test_retries import OK, scripted

pytestmark = pytest.mark.integration


class RedisProxy:
    """Forwards TCP to the real Redis; can be stopped, restarted and black-holed."""

    def __init__(self, host: str, port: int) -> None:
        self._target = (host, port)
        self._server: asyncio.Server | None = None
        self._conns: set[asyncio.StreamWriter] = set()
        self.port = 0
        self.blackhole = False

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for writer in list(self._conns):  # first: wait_closed() waits for open connections
            writer.close()
        self._conns.clear()
        if server is not None:
            await asyncio.wait_for(server.wait_closed(), timeout=5)

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._conns.add(writer)
        try:
            if self.blackhole:
                await asyncio.sleep(3600)
                return
            up_reader, up_writer = await asyncio.open_connection(*self._target)
            self._conns.add(up_writer)

            async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
                try:
                    while data := await src.read(65536):
                        dst.write(data)
                        await dst.drain()
                except (ConnectionError, asyncio.CancelledError):
                    pass
                finally:
                    dst.close()

            await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            self._conns.discard(writer)


@pytest.fixture
async def proxy() -> AsyncIterator[RedisProxy]:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.fail("REDIS_URL must point at a Redis server to run integration tests")
    parsed = urlparse(url)
    p = RedisProxy(parsed.hostname or "127.0.0.1", parsed.port or 6379)
    await p.start()
    yield p
    await p.stop()


class Stack:
    def __init__(self) -> None:
        self.latencies: list[tuple[float, float, int]] = []  # (finished at, seconds, status)


def _p99(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.99 * (len(ordered) - 1)))]


async def _build(
    make_gateway: GatewayFactory, proxy: RedisProxy, upstream_port: int, api_id: str, **kw: Any
) -> tuple[Any, RedisHealth, InMemoryTelemetrySink]:
    clock = SystemClock()
    sink = InMemoryTelemetrySink()
    health = RedisHealth.from_url(proxy.url, clock=clock, sink=sink, probe_interval_s=0.5)
    circuits = RedisCircuitStore(proxy.url, clock=clock)
    entry = api_entry(
        api_id,
        "none",
        {"mode": "none"},
        upstream_port,
        rate_limits={
            "inbound": {"enabled": True, "capacity": 100000, "refill_per_s": 100000},
            "upstream": {"enabled": True, "capacity": 100000, "refill_per_s": 100000},
        },
    )
    gateway = make_gateway(
        apis=[entry],
        redis_health=health,
        events=sink,
        limiter=FallbackRateLimiter(
            RedisRateLimiter(proxy.url, clock=clock),
            InMemoryRateLimiter(clock),
            clock=clock,
            health=health,
        ),
        circuit_breaker=CircuitBreaker(
            FallbackCircuitStore(circuits, InMemoryCircuitStore(clock), clock=clock, health=health),
            clock=clock,
            sink=sink,
            publish=circuits.publish_change,
        ),
        retry_budget=RetryBudget(
            RedisBudgetBackend(proxy.url), clock=clock, sink=sink, health=health
        ),
        **kw,
    )
    return gateway, health, sink


async def _load(gateway: Any, stack: Stack, stop: asyncio.Event, workers: int = 8) -> None:
    async def worker() -> None:
        while not stop.is_set():
            begin = time.monotonic()
            try:
                status = (await gateway.call("GET", "/gw/none/x")).status_code
            except Exception:
                status = 0
            stack.latencies.append((time.monotonic(), time.monotonic() - begin, status))
            await asyncio.sleep(0.002)

    await asyncio.gather(*(worker() for _ in range(workers)))


def _log_results(**values: float) -> None:
    """Merge the numbers into $BENCH_DIR/results.json (default build/bench), like the benchmarks."""
    from pathlib import Path

    from benchlib import read_json, write_json

    path = Path(os.environ.get("BENCH_DIR", "build/bench")) / "results.json"
    results = read_json(path) if path.exists() else {}
    for name, value in values.items():
        results[name] = round(float(value), 3)
    write_json(path, results)


async def _redis_keys(prefix: str) -> list[bytes]:
    client = redis_asyncio.from_url(os.environ["REDIS_URL"])
    try:
        keys: list[bytes] = await client.keys(prefix)
        return keys
    finally:
        await client.aclose()


@pytest.mark.req("REQ-2-3", "REQ-8-1")
async def test_killing_and_restoring_redis_under_load_causes_no_jarvis_errors(
    make_gateway: GatewayFactory, proxy: RedisProxy
) -> None:
    api_id = f"outage_{uuid.uuid4().hex[:8]}"
    async with scripted(OK) as upstream:
        gateway, health, sink = await _build(make_gateway, proxy, upstream.port, api_id)
        stack, stop = Stack(), asyncio.Event()
        load = asyncio.create_task(_load(gateway, stack, stop))
        await asyncio.sleep(2.0)  # normal
        down_at = time.monotonic()
        await proxy.stop()  # kill Redis
        await asyncio.sleep(0.5)
        ready = (await gateway.client.get("/readyz")).json()
        await asyncio.sleep(2.5)  # outage
        up_at = time.monotonic()
        await proxy.start()  # restore it
        for _ in range(100):  # the stack must recover within 5 s
            if health.available:
                break
            await asyncio.sleep(0.05)
        recovered_in = time.monotonic() - up_at
        await asyncio.sleep(1.0)  # and keep serving, using Redis again
        stop.set()
        await load
        keys = await _redis_keys(f"{LIMIT_PREFIX}in:*{api_id}*")
        await _redis_keys(f"{CIRCUIT_PREFIX}{api_id}*")
        await gateway.forwarder.aclose()

    statuses = [s for _, _, s in stack.latencies]
    assert len(statuses) > 200
    assert set(statuses) == {200}  # not one request failed because of JARVIS
    normal = [lat for t, lat, _ in stack.latencies if t < down_at]
    outage = [lat for t, lat, _ in stack.latencies if down_at + 0.3 < t < up_at]
    assert normal
    assert outage
    assert _p99(outage) <= max(2 * _p99(normal), 0.05)  # 2x normal p99 (floor: timer noise)
    assert recovered_in <= 5.0
    _log_results(
        redis_outage_errors=sum(1 for st in statuses if st != 200),
        redis_outage_recovery_s=recovered_in,
        redis_normal_p99_ms=_p99(normal) * 1000,
        redis_outage_p99_ms=_p99(outage) * 1000,
    )
    assert ready["status"] == "degraded"  # /readyz said so during the outage
    assert ready["redis"] == {"mode": "degraded"}
    assert keys  # after recovery the shared limiter bucket is in Redis again
    events = [
        e for e in sink.events if isinstance(e, dict) and e.get("type") == "redis_unavailable"
    ]
    assert len(events) == 1
    assert events[0]["duration_s"] >= 2.5


@pytest.mark.req("REQ-2-3")
async def test_a_black_holed_redis_costs_only_the_first_few_requests(
    make_gateway: GatewayFactory, proxy: RedisProxy
) -> None:
    api_id = f"hole_{uuid.uuid4().hex[:8]}"
    async with scripted(OK) as upstream:
        gateway, health, _ = await _build(make_gateway, proxy, upstream.port, api_id)
        await proxy.stop()
        proxy.blackhole = True
        await proxy.start()  # accepts connections, never answers
        started = time.monotonic()
        first = [await gateway.call("GET", "/gw/none/x") for _ in range(6)]
        assert all(r.status_code == 200 for r in first)  # slow at first, never failed
        assert time.monotonic() - started < 2.0
        assert not health.available
        later = []
        for _ in range(100):
            begin = time.monotonic()
            assert (await gateway.call("GET", "/gw/none/x")).status_code == 200
            later.append(time.monotonic() - begin)
        assert _p99(later) < 0.1  # Redis is skipped now: no waiting for its timeouts
        assert time.monotonic() - started < 10
        await gateway.forwarder.aclose()


@pytest.mark.req("REQ-2-4")
async def test_with_fail_open_off_requests_are_refused_while_redis_is_down(
    make_gateway: GatewayFactory, proxy: RedisProxy
) -> None:
    api_id = f"closed_{uuid.uuid4().hex[:8]}"
    async with scripted(OK) as upstream:
        gateway, health, _ = await _build(
            make_gateway, proxy, upstream.port, api_id, redis_fail_open=False
        )
        assert (await gateway.call("GET", "/gw/none/x")).status_code == 200
        await proxy.stop()
        for _ in range(10):  # the first calls find Redis down, then it is known
            await gateway.call("GET", "/gw/none/x")
        assert not health.available
        refused = await gateway.call("GET", "/gw/none/x")
        assert refused.status_code == 503
        assert refused.json()["error"]["code"] == "JV_NOT_READY"
        await proxy.start()
        for _ in range(100):
            if health.available:
                break
            await asyncio.sleep(0.05)
        assert (await gateway.call("GET", "/gw/none/x")).status_code == 200
        await gateway.forwarder.aclose()
