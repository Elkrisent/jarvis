"""Micro-benchmark: one in-memory token bucket take (metric ``token_bucket_take``).

pytest-benchmark names the metric after the test, minus the ``test_bench_`` prefix.
"""

from collections.abc import Coroutine
from typing import Any

import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.memory import InMemoryRateLimiter
from pytest_benchmark.fixture import BenchmarkFixture

pytestmark = pytest.mark.perf


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive a coroutine that never suspends, without an event loop's own overhead."""
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value  # type: ignore[no-any-return]
    raise AssertionError("coroutine suspended")


def test_bench_token_bucket_take(benchmark: BenchmarkFixture) -> None:
    limiter = InMemoryRateLimiter(FakeClock())

    def take() -> None:
        _run(limiter.take("bench", capacity=10**9, refill_per_s=1.0))

    benchmark(take)
