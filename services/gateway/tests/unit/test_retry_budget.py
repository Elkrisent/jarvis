from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.memory import InMemoryTelemetrySink
from jarvis_gateway.retry_budget import RetryBudget

pytestmark = pytest.mark.unit

CTX: dict[str, Any] = {"project_id": "p1", "request_id": "r1"}


class FakeBackend:
    def __init__(self, *, fail: bool = False, answer: bool = True) -> None:
        self.fail = fail
        self.answer = answer
        self.acquired: list[tuple[str, int, int, float]] = []
        self.added: list[tuple[str, int]] = []
        self.closed = False

    async def add_requests(self, api_id: str, now_s: int, count: int) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.added.append((api_id, count))

    async def acquire(self, api_id: str, now_s: int, pending: int, ratio: float) -> bool:
        if self.fail:
            raise ConnectionError("redis down")
        self.acquired.append((api_id, now_s, pending, ratio))
        return self.answer

    async def aclose(self) -> None:
        self.closed = True


async def _spend(budget: RetryBudget, api: str = "a1", ratio: float = 0.2) -> bool:
    return await budget.try_acquire(api, ratio, **CTX)


@pytest.mark.req("REQ-4.7-7", "REQ-4.7-8")
async def test_local_budget_allows_retries_up_to_the_ratio_of_requests() -> None:
    budget = RetryBudget(None, clock=FakeClock())
    for _ in range(10):
        budget.record_request("a1")
    assert await _spend(budget) is True  # 1 <= 0.2 * 10
    assert await _spend(budget) is True  # 2 <= 2.0 (float noise must not flip this edge)
    assert await _spend(budget) is False  # 3 > 2.0
    assert await _spend(budget) is False


@pytest.mark.req("REQ-4.7-8")
async def test_no_requests_means_no_retries() -> None:
    assert await _spend(RetryBudget(None, clock=FakeClock())) is False


@pytest.mark.req("REQ-4.7-7")
async def test_the_window_is_rolling_60_seconds() -> None:
    clock = FakeClock()
    budget = RetryBudget(None, clock=clock)
    for _ in range(5):
        budget.record_request("a1")
    assert await _spend(budget) is True
    assert await _spend(budget) is False
    clock.advance(59)
    assert await _spend(budget) is False  # still inside the window
    clock.advance(2)  # the first second has left the window: no requests, no retries counted
    assert await _spend(budget) is False
    for _ in range(5):
        budget.record_request("a1")
    assert await _spend(budget) is True  # a fresh window


async def test_budgets_are_per_api() -> None:
    budget = RetryBudget(None, clock=FakeClock())
    for _ in range(5):
        budget.record_request("busy")
    assert await _spend(budget, "busy") is True
    assert await _spend(budget, "quiet") is False


@pytest.mark.req("REQ-4.7-7")
async def test_the_shared_backend_decides_and_gets_the_unflushed_request_count() -> None:
    backend = FakeBackend(answer=True)
    budget = RetryBudget(backend, clock=FakeClock(), flush_interval_s=3600)
    for _ in range(3):
        budget.record_request("a1")
    assert await _spend(budget, ratio=0.5) is True
    assert backend.acquired == [("a1", 1_000_000, 3, 0.5)]
    await _spend(budget)
    assert backend.acquired[1][2] == 0  # already handed over, not sent twice
    backend.answer = False
    assert await _spend(budget) is False  # the shared figure wins over the local one
    await budget.aclose()


@pytest.mark.req("REQ-4.7-7")
async def test_requests_are_flushed_in_the_background() -> None:
    backend = FakeBackend()
    budget = RetryBudget(backend, clock=FakeClock(), flush_interval_s=0.01)
    for _ in range(4):
        budget.record_request("a1")
    await asyncio.sleep(0.1)
    assert backend.added == [("a1", 4)]
    await budget.aclose()
    assert backend.closed


async def test_aclose_flushes_what_is_left() -> None:
    backend = FakeBackend()
    budget = RetryBudget(backend, clock=FakeClock(), flush_interval_s=3600)
    budget.record_request("a1")
    budget.record_request("a2")
    await budget.aclose()
    assert sorted(backend.added) == [("a1", 1), ("a2", 1)]


@pytest.mark.req("REQ-4.7-7")
async def test_with_redis_down_the_instance_falls_back_to_its_own_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = FakeBackend(fail=True)
    budget = RetryBudget(backend, clock=FakeClock(), flush_interval_s=3600)
    for _ in range(5):
        budget.record_request("a1")
    with caplog.at_level(logging.WARNING):
        assert await _spend(budget) is True
        assert await _spend(budget) is False
    assert caplog.text.count("Redis unavailable") == 1  # rate-limited
    assert "redis down" not in caplog.text
    # The unflushed requests were kept for when Redis returns.
    backend.fail = False
    await budget.aclose()
    assert backend.added == [("a1", 5)]


async def test_a_flush_that_fails_keeps_the_count_and_does_not_stop_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = FakeBackend(fail=True)
    budget = RetryBudget(backend, clock=FakeClock(), flush_interval_s=0.01)
    budget.record_request("a1")
    with caplog.at_level(logging.WARNING):
        await asyncio.sleep(0.05)
    backend.fail = False
    await asyncio.sleep(0.05)
    assert backend.added == [("a1", 1)]
    await budget.aclose()


async def test_a_slow_backend_is_cut_off_and_the_local_rule_applies() -> None:
    class Slow(FakeBackend):
        async def acquire(self, api_id: str, now_s: int, pending: int, ratio: float) -> bool:
            await asyncio.sleep(5)
            return True

    budget = RetryBudget(Slow(), clock=FakeClock(), command_timeout_s=0.02, flush_interval_s=3600)
    assert await _spend(budget) is False  # no local requests recorded, so nothing to spend
    await budget.aclose()


@pytest.mark.req("REQ-4.7-8", "REQ-6.1-3")
async def test_exhaustion_emits_one_event_per_api_per_interval() -> None:
    clock = FakeClock()
    sink = InMemoryTelemetrySink()
    budget = RetryBudget(None, clock=clock, sink=sink, event_interval_s=1.0)
    assert await _spend(budget) is False
    assert await _spend(budget) is False  # same second: no second event
    assert len(sink.events) == 1
    event = sink.events[0]
    assert isinstance(event, dict)
    assert event["kind"] == "event"
    assert event["type"] == "retry_budget_exhausted"
    assert event["api_id"] == "a1"
    assert event["project_id"] == "p1"
    assert event["request_id"] == "r1"
    assert event["budget_ratio"] == 0.2
    assert str(event["ts"]).endswith("Z")
    assert await _spend(budget, "a2") is False  # another API has its own throttle
    clock.advance(1.5)
    assert await _spend(budget) is False
    assert len(sink.events) == 3


async def test_exhaustion_without_a_sink_is_fine() -> None:
    assert await _spend(RetryBudget(None, clock=FakeClock())) is False


async def test_record_after_close_does_not_restart_the_flusher() -> None:
    backend = FakeBackend()
    budget = RetryBudget(backend, clock=FakeClock())
    await budget.aclose()
    budget.record_request("a1")
    assert budget._flusher is None


async def test_aclose_without_a_backend_and_the_repr_hides_the_url() -> None:
    from jarvis_gateway.retry_budget import RedisBudgetBackend

    await RetryBudget(None, clock=FakeClock()).aclose()
    backend = RedisBudgetBackend("redis://user:hunter2@127.0.0.1:1/0")
    assert "hunter2" not in repr(backend)
    await backend.aclose()
