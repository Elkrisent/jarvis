from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.config import CircuitPolicy
from jarvis_core.interfaces import CircuitSnapshot, CircuitState
from jarvis_core.memory import InMemoryCircuitStore, InMemoryTelemetrySink
from jarvis_gateway.circuit import CircuitBreaker

pytestmark = pytest.mark.unit

POLICY = CircuitPolicy()
API = "api_1"
PROJECT = "prj_1"


class Harness:
    def __init__(self, store: InMemoryCircuitStore | None = None) -> None:
        self.clock = FakeClock()
        self.store = store or InMemoryCircuitStore(self.clock)
        self.sink = InMemoryTelemetrySink()
        self.published: list[str] = []
        self.breaker = self.make(self.store)

    def make(self, store: Any) -> CircuitBreaker:
        async def publish(api_id: str) -> None:
            self.published.append(api_id)

        return CircuitBreaker(
            store, clock=self.clock, sink=self.sink, publish=publish, refresh_interval_s=3600
        )

    async def admit(self, breaker: CircuitBreaker | None = None) -> Any:
        return await (breaker or self.breaker).admit(API, POLICY, PROJECT, probe_ttl_s=10)

    async def calls(self, ok: int, failed: int, breaker: CircuitBreaker | None = None) -> None:
        target = breaker or self.breaker
        for _ in range(ok):
            target.record(API, POLICY, PROJECT, failure=False)
        for _ in range(failed):
            target.record(API, POLICY, PROJECT, failure=True)
        await target.drain()

    async def open_it(self) -> None:
        await self.admit()  # first sight
        await self.calls(0, 20)
        assert self.breaker.state(API) is CircuitState.OPEN

    def events(self) -> list[dict[str, Any]]:
        return [e for e in self.sink.events if isinstance(e, dict)]


@pytest.fixture
async def h() -> Harness:
    return Harness()


@pytest.mark.req("REQ-4.8-4")
async def test_enough_failures_open_the_circuit_and_emit_an_event(h: Harness) -> None:
    assert (await h.admit()).action == "allow"
    await h.calls(0, 19)
    assert h.breaker.state(API) is CircuitState.CLOSED  # under min_calls
    await h.calls(0, 1)
    assert h.breaker.state(API) is CircuitState.OPEN
    assert (await h.store.get(API)).state is CircuitState.OPEN
    assert h.published == [API]
    [event] = h.events()
    assert event["kind"] == "event"
    assert event["type"] == "circuit_transition"
    assert (event["from"], event["to"]) == ("CLOSED", "OPEN")
    assert event["api_id"] == API
    assert event["project_id"] == PROJECT


@pytest.mark.req("REQ-4.8-3")
async def test_successes_keep_the_rate_below_the_threshold(h: Harness) -> None:
    await h.admit()
    await h.calls(11, 9)  # 45 %
    assert h.breaker.state(API) is CircuitState.CLOSED
    await h.calls(0, 1)  # 10 / 21 = 47.6 %
    await h.calls(0, 2)  # 12 / 23 = 52 %
    assert h.breaker.state(API) is CircuitState.OPEN


@pytest.mark.req("REQ-4.8-10")
async def test_an_open_circuit_rejects_until_the_open_duration_passes(h: Harness) -> None:
    await h.open_it()
    assert (await h.admit()).action == "reject"
    h.clock.advance(29)
    ticket = await h.admit()
    assert ticket.action == "reject"
    assert ticket.state is CircuitState.OPEN
    h.clock.advance(1)
    assert (await h.admit()).action == "probe"
    assert h.breaker.state(API) is CircuitState.HALF_OPEN


@pytest.mark.req("REQ-4.8-6")
async def test_exactly_one_probe_is_let_through_under_concurrency(h: Harness) -> None:
    await h.open_it()
    h.clock.advance(31)
    tickets = await asyncio.gather(*(h.admit() for _ in range(50)))
    assert [t.action for t in tickets].count("probe") == 1
    assert [t.action for t in tickets].count("reject") == 49
    assert {t.state for t in tickets if t.action == "reject"} <= {
        CircuitState.HALF_OPEN,
        CircuitState.OPEN,
    }
    transitions = [(e["from"], e["to"]) for e in h.events()]
    assert transitions == [("CLOSED", "OPEN"), ("OPEN", "HALF_OPEN")]


@pytest.mark.req("REQ-4.8-7", "REQ-4.8-9")
async def test_a_successful_probe_closes_the_circuit_and_old_failures_are_forgotten(
    h: Harness,
) -> None:
    await h.open_it()
    h.clock.advance(31)
    assert (await h.admit()).action == "probe"
    await h.breaker.finish_probe(API, POLICY, PROJECT, "success")
    assert h.breaker.state(API) is CircuitState.CLOSED
    assert (await h.admit()).action == "allow"
    assert (await h.store.get(API)).state is CircuitState.CLOSED
    # The 20 old failures are still in the store's window but must not re-trip the circuit.
    await h.calls(5, 0)
    assert h.breaker.state(API) is CircuitState.CLOSED
    assert [(e["from"], e["to"]) for e in h.events()][-1] == ("HALF_OPEN", "CLOSED")


@pytest.mark.req("REQ-4.8-8", "REQ-4.8-9")
async def test_a_failed_probe_reopens_for_twice_as_long_up_to_the_cap(h: Harness) -> None:
    await h.open_it()
    durations = []
    for _ in range(5):
        h.clock.advance(301)
        assert (await h.admit()).action == "probe"
        await h.breaker.finish_probe(API, POLICY, PROJECT, "failure")
        snap = await h.store.get(API)
        assert snap.state is CircuitState.OPEN
        durations.append(snap.open_duration_s)
        assert (await h.admit()).action == "reject"
    assert durations == [60, 120, 240, 300, 300]
    assert h.events()[-1]["reason"] == "probe_failed"


async def test_a_cancelled_probe_gives_the_slot_back_without_a_verdict(h: Harness) -> None:
    await h.open_it()
    h.clock.advance(31)
    assert (await h.admit()).action == "probe"
    assert (await h.admit()).action == "reject"  # the probe is in flight
    await h.breaker.finish_probe(API, POLICY, PROJECT, "cancelled")
    assert (await h.store.get(API)).state is CircuitState.HALF_OPEN
    assert (await h.admit()).action == "probe"  # the next request becomes the probe


@pytest.mark.req("REQ-4.8-1")
async def test_a_second_instance_sees_the_same_state_after_a_refresh(h: Harness) -> None:
    other = h.make(h.store)
    assert (await h.admit(other)).action == "allow"  # first sight: CLOSED
    await h.open_it()
    assert other.state(API) is CircuitState.CLOSED  # not yet refreshed
    await other.refresh()
    assert other.state(API) is CircuitState.OPEN
    assert (await h.admit(other)).action == "reject"
    h.clock.advance(31)
    await h.store.set(API, CircuitSnapshot(CircuitState.CLOSED, closed_at=h.clock.now()))
    await other.on_remote_change(API)  # a pub/sub push
    assert other.state(API) is CircuitState.CLOSED
    await other.aclose()


@pytest.mark.req("REQ-4.8-1")
async def test_a_new_instance_reads_the_shared_state_on_first_sight(h: Harness) -> None:
    await h.open_it()
    fresh = h.make(h.store)
    assert (await h.admit(fresh)).action == "reject"
    await fresh.aclose()


async def test_two_instances_do_not_both_trip_it(h: Harness) -> None:
    other = h.make(h.store)
    await h.admit()
    await h.admit(other)
    await h.calls(0, 20)
    await h.calls(0, 5, other)  # `other` still thinks CLOSED, finds it OPEN in the store
    assert other.state(API) is CircuitState.OPEN
    assert len([e for e in h.events() if e["to"] == "OPEN"]) == 1
    await other.aclose()


async def test_a_circuit_closed_by_someone_else_is_noticed_when_taking_the_probe_lock(
    h: Harness,
) -> None:
    await h.open_it()
    h.clock.advance(31)
    # Another instance closed it; this instance's mirror still says OPEN and past its duration.
    await h.store.set(API, CircuitSnapshot(CircuitState.CLOSED, closed_at=h.clock.now()))
    ticket = await h.admit()
    assert ticket.action == "allow"
    assert h.breaker.state(API) is CircuitState.CLOSED
    assert await h.store.try_acquire_probe(API, ttl_s=10)  # the slot was given back


async def test_a_broken_store_fails_open_and_logs_only_the_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Broken(InMemoryCircuitStore):
        async def get(self, api_id: str) -> CircuitSnapshot:
            raise ConnectionError("SECRET-MARKER")

    h = Harness(Broken(FakeClock()))
    with caplog.at_level(logging.WARNING):
        ticket = await h.admit()
    assert ticket.action == "allow"
    assert "ConnectionError" in caplog.text
    assert "SECRET-MARKER" not in caplog.text


async def test_the_background_tasks_count_outcomes_and_refresh_the_mirror() -> None:
    h = Harness()
    breaker = CircuitBreaker(h.store, clock=h.clock, sink=h.sink, refresh_interval_s=0.01)
    await breaker.admit(API, POLICY, PROJECT, probe_ttl_s=10)
    for _ in range(20):
        breaker.record(API, POLICY, PROJECT, failure=True)
    for _ in range(100):
        if breaker.state(API) is CircuitState.OPEN:
            break
        await asyncio.sleep(0.01)
    assert breaker.state(API) is CircuitState.OPEN
    await h.store.set(API, CircuitSnapshot())  # reset behind its back
    for _ in range(100):
        if breaker.state(API) is CircuitState.CLOSED:
            break
        await asyncio.sleep(0.01)
    assert breaker.state(API) is CircuitState.CLOSED  # the refresh loop picked it up
    await breaker.aclose()


async def test_a_listener_is_started_and_stopped_with_the_breaker() -> None:
    h = Harness()
    seen: list[str] = []
    started = asyncio.Event()

    async def listen(on_change: Any, stop: asyncio.Event) -> None:
        started.set()
        await on_change("api_x")
        seen.append("changed")
        await stop.wait()

    breaker = CircuitBreaker(h.store, clock=h.clock, listen=listen, refresh_interval_s=3600)
    await breaker.admit(API, POLICY, PROJECT, probe_ttl_s=10)
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.sleep(0.01)
    assert seen == ["changed"]
    assert breaker.state("api_x") is CircuitState.CLOSED
    await breaker.aclose()


async def test_aclose_counts_what_is_queued_and_closes_the_store() -> None:
    closed: list[bool] = []

    class Store(InMemoryCircuitStore):
        async def aclose(self) -> None:
            closed.append(True)

    clock = FakeClock()
    breaker = CircuitBreaker(Store(clock), clock=clock, refresh_interval_s=3600)
    await breaker.admit(API, POLICY, PROJECT, probe_ttl_s=10)
    breaker.record(API, POLICY, PROJECT, failure=True)
    await breaker.aclose()
    assert closed == [True]
    breaker.record(API, POLICY, PROJECT, failure=True)  # after close: queued, nothing restarts
    assert breaker.is_open(API) is False


async def test_a_publish_failure_is_not_fatal(h: Harness) -> None:
    async def failing(api_id: str) -> None:
        raise ConnectionError("down")

    breaker = CircuitBreaker(h.store, clock=h.clock, publish=failing, refresh_interval_s=3600)
    await breaker.admit(API, POLICY, PROJECT, probe_ttl_s=10)
    for _ in range(20):
        breaker.record(API, POLICY, PROJECT, failure=True)
    await breaker.drain()
    assert breaker.state(API) is CircuitState.OPEN
    await breaker.aclose()


async def test_a_store_without_aclose_is_fine() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(InMemoryCircuitStore(clock), clock=clock)
    await breaker.aclose()


async def test_a_cancelled_admit_propagates() -> None:
    class Cancelling(InMemoryCircuitStore):
        async def get(self, api_id: str) -> CircuitSnapshot:
            raise asyncio.CancelledError

    h = Harness(Cancelling(FakeClock()))
    with pytest.raises(asyncio.CancelledError):
        await h.admit()


async def test_a_store_failure_while_counting_is_logged_and_does_not_stop_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Flaky(InMemoryCircuitStore):
        fail = True

        async def record_call(self, api_id: str, *, success: bool, window_s: int) -> Any:
            if self.fail:
                raise ConnectionError("SECRET-MARKER")
            return await super().record_call(api_id, success=success, window_s=window_s)

    clock = FakeClock()
    store = Flaky(clock)
    breaker = CircuitBreaker(store, clock=clock, refresh_interval_s=3600)
    await breaker.admit(API, POLICY, PROJECT, probe_ttl_s=10)
    with caplog.at_level(logging.WARNING):
        breaker.record(API, POLICY, PROJECT, failure=True)
        await asyncio.sleep(0.05)
    assert "outcome counting failed (ConnectionError)" in caplog.text
    assert "SECRET-MARKER" not in caplog.text
    store.fail = False
    for _ in range(20):
        breaker.record(API, POLICY, PROJECT, failure=True)
    for _ in range(100):
        if breaker.state(API) is CircuitState.OPEN:
            break
        await asyncio.sleep(0.01)
    assert breaker.state(API) is CircuitState.OPEN  # the loop survived the failure
    await breaker.aclose()


async def test_a_store_failure_while_refreshing_is_logged_and_does_not_stop_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Flaky(InMemoryCircuitStore):
        fail = False

        async def get(self, api_id: str) -> CircuitSnapshot:
            if self.fail:
                raise ConnectionError("SECRET-MARKER")
            return await super().get(api_id)

    clock = FakeClock()
    store = Flaky(clock)
    breaker = CircuitBreaker(store, clock=clock, refresh_interval_s=0.01)
    await breaker.admit(API, POLICY, PROJECT, probe_ttl_s=10)
    store.fail = True
    with caplog.at_level(logging.WARNING):
        await asyncio.sleep(0.05)
    assert "circuit refresh failed (ConnectionError)" in caplog.text
    assert "SECRET-MARKER" not in caplog.text
    store.fail = False
    await store.set(API, CircuitSnapshot(CircuitState.OPEN, clock.now(), 30))
    for _ in range(100):
        if breaker.state(API) is CircuitState.OPEN:
            break
        await asyncio.sleep(0.01)
    assert breaker.state(API) is CircuitState.OPEN
    await breaker.aclose()


async def test_aclose_survives_a_store_that_fails_while_draining() -> None:
    class Broken(InMemoryCircuitStore):
        async def record_call(self, api_id: str, *, success: bool, window_s: int) -> Any:
            raise ConnectionError("down")

    clock = FakeClock()
    breaker = CircuitBreaker(Broken(clock), clock=clock, refresh_interval_s=3600)
    await breaker.admit(API, POLICY, PROJECT, probe_ttl_s=10)
    breaker._queue.append((API, POLICY, PROJECT, True))
    await breaker.aclose()  # must not raise


async def test_fallback_store_uses_local_state_when_redis_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from jarvis_gateway.circuit_store import FallbackCircuitStore

    class Down:
        async def get(self, api_id: str) -> CircuitSnapshot:
            raise ConnectionError("SECRET-MARKER")

        async def set(self, api_id: str, snapshot: CircuitSnapshot) -> None:
            raise ConnectionError("SECRET-MARKER")

        async def record_call(self, api_id: str, *, success: bool, window_s: int) -> Any:
            raise ConnectionError("SECRET-MARKER")

        async def try_acquire_probe(self, api_id: str, *, ttl_s: int) -> bool:
            raise ConnectionError("SECRET-MARKER")

        async def release_probe(self, api_id: str) -> None:
            raise ConnectionError("SECRET-MARKER")

    clock = FakeClock()
    store = FallbackCircuitStore(Down(), InMemoryCircuitStore(clock), clock=clock)
    with caplog.at_level(logging.WARNING):
        assert (await store.get("a")).state is CircuitState.CLOSED
        snap = CircuitSnapshot(CircuitState.OPEN, 1.0, 30)
        await store.set("a", snap)
        assert await store.get("a") == snap
        stats = await store.record_call("a", success=False, window_s=30)
        assert (stats.total, stats.failures) == (1, 1)
        assert await store.try_acquire_probe("a", ttl_s=10) is True
        assert await store.try_acquire_probe("a", ttl_s=10) is False
        await store.release_probe("a")
        assert await store.try_acquire_probe("a", ttl_s=10) is True
    assert caplog.text.count("Redis unavailable") == 1  # rate-limited
    assert "SECRET-MARKER" not in caplog.text


async def test_fallback_store_prefers_redis_when_it_works() -> None:
    from jarvis_gateway.circuit_store import FallbackCircuitStore

    clock = FakeClock()
    primary, local = InMemoryCircuitStore(clock), InMemoryCircuitStore(clock)
    store = FallbackCircuitStore(primary, local, clock=clock)
    await store.set("a", CircuitSnapshot(CircuitState.OPEN, 1.0, 30))
    assert (await primary.get("a")).state is CircuitState.OPEN
    assert (await local.get("a")).state is CircuitState.CLOSED
