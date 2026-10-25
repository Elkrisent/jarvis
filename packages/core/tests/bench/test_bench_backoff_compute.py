"""Micro-benchmark: one retry decision with full-jitter backoff (metric ``backoff_compute``)."""

import pytest
from jarvis_core.clock import FakeClock, SystemRandom
from jarvis_core.config import RetryPolicy
from jarvis_core.deadline import Deadline
from jarvis_core.retry import Failure, decide
from pytest_benchmark.fixture import BenchmarkFixture

pytestmark = pytest.mark.perf


def test_bench_backoff_compute(benchmark: BenchmarkFixture) -> None:
    clock = FakeClock()
    policy = RetryPolicy()
    deadline = Deadline(clock, clock.monotonic(), 5000)
    rng = SystemRandom()
    failure = Failure("status", 503, "1")

    def decision() -> None:
        decide(
            policy,
            method="GET",
            failure=failure,
            attempts_made=1,
            deadline=deadline,
            min_attempt_ms=300,
            rng=rng,
            now_epoch=1_000_000.0,
        )

    benchmark(decision)
