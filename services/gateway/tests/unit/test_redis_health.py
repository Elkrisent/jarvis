from __future__ import annotations

import asyncio
import logging

import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.memory import InMemoryCircuitStore, InMemoryRateLimiter, InMemoryTelemetrySink
from jarvis_gateway.circuit_store import FallbackCircuitStore
from jarvis_gateway.rate_limit import FallbackRateLimiter
from jarvis_gateway.redis_health import RedisHealth, RedisUnavailable
from jarvis_gateway.retry_budget import RetryBudget

pytestmark = pytest.mark.unit


class Redis:
    """A scriptable Redis: ``up`` flips it; ``calls`` counts what reached it."""

    def __init__(self) -> None:
        self.up = True
        self.calls = 0
        self.pings = 0

    async def operation(self) -> str:
        self.calls += 1
        if not self.up:
            raise ConnectionError("SECRET-MARKER")
        return "ok"

    async def ping(self) -> None:
        self.pings += 1
        if not self.up:
            raise ConnectionError("down")


def _health(redis: Redis, **kw: object) -> RedisHealth:
    return RedisHealth(redis.ping, clock=FakeClock(), probe_interval_s=0.01, **kw)  # type: ignore[arg-type]


@pytest.mark.req("REQ-2-3")
async def test_redis_counts_as_down_after_the_threshold_of_failures_in_a_row() -> None:
    redis, health = Redis(), None
    health = _health(redis, failure_threshold=3)
    redis.up = False
    for _ in range(2):
        with pytest.raises(ConnectionError):
            await health.call(redis.operation)
    assert health.available
    with pytest.raises(ConnectionError):
        await health.call(redis.operation)
    assert not health.available
    assert health.down_since is not None
    await health.aclose()


async def test_a_success_resets_the_failure_count() -> None:
    redis = Redis()
    health = _health(redis, failure_threshold=2)
    redis.up = False
    with pytest.raises(ConnectionError):
        await health.call(redis.operation)
    redis.up = True
    assert await health.call(redis.operation) == "ok"
    redis.up = False
    with pytest.raises(ConnectionError):
        await health.call(redis.operation)
    assert health.available  # one failure since the last success, not two


@pytest.mark.req("REQ-2-3")
async def test_while_down_calls_are_refused_at_once_without_touching_redis() -> None:
    redis = Redis()
    health = _health(redis, failure_threshold=1)
    redis.up = False
    with pytest.raises(ConnectionError):
        await health.call(redis.operation)
    calls = redis.calls
    for _ in range(50):
        with pytest.raises(RedisUnavailable):
            await health.call(redis.operation)
    assert redis.calls == calls
    await health.aclose()


@pytest.mark.req("REQ-2-3")
async def test_a_background_ping_brings_it_back_and_one_event_records_the_outage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis = Redis()
    sink = InMemoryTelemetrySink()
    clock = FakeClock()
    health = RedisHealth(
        redis.ping, clock=clock, sink=sink, failure_threshold=1, probe_interval_s=0.01
    )
    redis.up = False
    with caplog.at_level(logging.WARNING), pytest.raises(ConnectionError):
        await health.call(redis.operation)
    await asyncio.sleep(0.05)
    assert not health.available
    assert redis.pings >= 1
    assert sink.events == []  # the event comes after recovery
    clock.advance(7.5)
    redis.up = True
    for _ in range(100):
        if health.available:
            break
        await asyncio.sleep(0.01)
    assert health.available
    [event] = sink.events
    assert isinstance(event, dict)
    assert event["kind"] == "event"
    assert event["type"] == "redis_unavailable"
    assert event["duration_s"] == 7.5
    assert str(event["ts"]).endswith("Z")
    assert str(event["down_since"]).endswith("Z")
    assert "SECRET-MARKER" not in caplog.text
    assert await health.call(redis.operation) == "ok"  # Redis is used again
    await health.aclose()


async def test_a_second_outage_is_tracked_again() -> None:
    redis = Redis()
    sink = InMemoryTelemetrySink()
    health = RedisHealth(
        redis.ping, clock=FakeClock(), sink=sink, failure_threshold=1, probe_interval_s=0.01
    )
    for _ in range(2):
        redis.up = False
        with pytest.raises(ConnectionError):
            await health.call(redis.operation)
        assert not health.available
        redis.up = True
        for _ in range(100):
            if health.available:
                break
            await asyncio.sleep(0.01)
        assert health.available
    assert len(sink.events) == 2
    await health.aclose()


async def test_a_hung_ping_does_not_stall_the_probe_loop() -> None:
    redis = Redis()

    async def hanging_ping() -> None:
        redis.pings += 1
        await asyncio.sleep(30)

    health = RedisHealth(
        hanging_ping,
        clock=FakeClock(),
        failure_threshold=1,
        probe_interval_s=0.01,
        ping_timeout_s=0.02,
    )
    redis.up = False
    with pytest.raises(ConnectionError):
        await health.call(redis.operation)
    await asyncio.sleep(0.2)
    assert redis.pings >= 3  # it keeps trying
    await health.aclose()


def test_the_threshold_must_be_positive() -> None:
    with pytest.raises(ValueError, match="failure_threshold"):
        RedisHealth(Redis().ping, failure_threshold=0)


async def test_failures_after_close_do_not_start_a_probe() -> None:
    closed: list[bool] = []

    async def closer() -> None:
        closed.append(True)

    health = RedisHealth(Redis().ping, failure_threshold=1, closer=closer)
    await health.aclose()
    health.record_failure()
    assert health.available
    assert closed == [True]
    assert repr(health) == "RedisHealth(available=True)"


async def test_from_url_pings_a_real_server_shape_without_connecting_at_construction() -> None:
    health = RedisHealth.from_url("redis://user:hunter2@127.0.0.1:1/0")
    assert "hunter2" not in repr(health)
    with pytest.raises(Exception):  # noqa: B017,PT011  (nothing listens on port 1)
        await health._ping()
    await health.aclose()


# --- the helpers skip Redis once it is known down ---------------------------------------------


class Failing:
    def __init__(self) -> None:
        self.calls = 0

    async def take(self, key: str, **kwargs: object) -> object:
        self.calls += 1
        raise ConnectionError("down")

    async def get(self, api_id: str) -> object:
        self.calls += 1
        raise ConnectionError("down")

    async def acquire(self, api_id: str, now_s: int, pending: int, ratio: float) -> bool:
        self.calls += 1
        raise ConnectionError("down")

    async def add_requests(self, api_id: str, now_s: int, count: int) -> None:
        self.calls += 1
        raise ConnectionError("down")

    async def aclose(self) -> None: ...


@pytest.mark.req("REQ-2-3", "REQ-4.11-4")
async def test_the_limiter_stops_calling_redis_once_it_is_known_down(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    redis = Failing()
    health = RedisHealth(Redis().ping, clock=clock, failure_threshold=2, probe_interval_s=3600)
    limiter = FallbackRateLimiter(redis, InMemoryRateLimiter(clock), clock=clock, health=health)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING):
        results = [await limiter.take("k", capacity=5, refill_per_s=1.0) for _ in range(8)]
    assert redis.calls == 2  # two failures tripped it; the other six never reached Redis
    assert [r.allowed for r in results] == [True] * 5 + [False] * 3  # the local bucket answered
    assert not health.available
    await health.aclose()


@pytest.mark.req("REQ-2-3")
async def test_the_circuit_store_stops_calling_redis_once_it_is_known_down() -> None:
    clock = FakeClock()
    redis = Failing()
    health = RedisHealth(Redis().ping, clock=clock, failure_threshold=1, probe_interval_s=3600)
    store = FallbackCircuitStore(redis, InMemoryCircuitStore(clock), clock=clock, health=health)  # type: ignore[arg-type]
    for _ in range(5):
        assert (await store.get("api")).state.value == "CLOSED"
    assert redis.calls == 1
    await health.aclose()


@pytest.mark.req("REQ-2-3")
async def test_the_retry_budget_stops_calling_redis_and_drops_outage_counts() -> None:
    clock = FakeClock()
    redis = Failing()
    health = RedisHealth(Redis().ping, clock=clock, failure_threshold=1, probe_interval_s=3600)
    budget = RetryBudget(redis, clock=clock, health=health, flush_interval_s=3600)  # type: ignore[arg-type]
    for _ in range(10):
        budget.record_request("a1")
    ctx = {"project_id": "p", "request_id": "r"}
    assert await budget.try_acquire("a1", 0.2, **ctx) is True  # local counts decide: 2 of 10
    assert await budget.try_acquire("a1", 0.2, **ctx) is True
    assert await budget.try_acquire("a1", 0.2, **ctx) is False
    assert redis.calls == 1
    assert not health.available
    await budget.aclose()  # the flush finds Redis down and drops the pending count quietly
    assert redis.calls == 1
    await health.aclose()


async def test_a_failing_flush_goes_through_the_health_tracker() -> None:
    clock = FakeClock()
    redis = Failing()
    health = RedisHealth(Redis().ping, clock=clock, failure_threshold=1, probe_interval_s=3600)
    budget = RetryBudget(redis, clock=clock, health=health, flush_interval_s=0.01)
    budget.record_request("a1")
    for _ in range(100):
        if not health.available:
            break
        await asyncio.sleep(0.01)
    assert not health.available
    await budget.aclose()
    await health.aclose()


@pytest.mark.req("REQ-2-3")
@pytest.mark.parametrize(
    "timeout",
    [TimeoutError(), __import__("redis").exceptions.TimeoutError("slow")],
    ids=["asyncio", "redis"],
)
async def test_one_timeout_counts_as_down_at_once(timeout: Exception) -> None:
    redis = Redis()
    health = _health(redis, failure_threshold=5)

    async def hang() -> None:
        raise timeout

    with pytest.raises(type(timeout)):
        await health.call(hang)
    assert not health.available  # a black-holed Redis would make every further call wait too
    await health.aclose()


async def test_aclose_without_a_closer_or_probe_is_fine() -> None:
    health = RedisHealth(Redis().ping)
    await health.aclose()
    await health.aclose()


async def test_recovery_without_a_sink_is_fine() -> None:
    redis = Redis()
    health = RedisHealth(redis.ping, clock=FakeClock(), failure_threshold=1, probe_interval_s=0.01)
    redis.up = False
    with pytest.raises(ConnectionError):
        await health.call(redis.operation)
    redis.up = True
    for _ in range(100):
        if health.available:
            break
        await asyncio.sleep(0.01)
    assert health.available
    await health.aclose()
