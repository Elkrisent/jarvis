from __future__ import annotations

import logging
from typing import Any

import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.interfaces import RateDecision
from jarvis_core.memory import InMemoryRateLimiter
from jarvis_gateway.rate_limit import FallbackRateLimiter, RedisRateLimiter

pytestmark = pytest.mark.unit


class Down:
    def __init__(self) -> None:
        self.calls = 0

    async def take(self, key: str, **kwargs: Any) -> RateDecision:
        self.calls += 1
        raise ConnectionError("SECRET-MARKER")


@pytest.mark.req("REQ-4.11-4")
async def test_with_redis_down_each_instance_gets_its_share_of_the_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    limiter = FallbackRateLimiter(
        Down(), InMemoryRateLimiter(clock), expected_instances=4, clock=clock
    )
    with caplog.at_level(logging.WARNING):
        results = [await limiter.take("k", capacity=40, refill_per_s=4.0) for _ in range(15)]
    assert [r.allowed for r in results] == [True] * 10 + [False] * 5  # 40 / 4 = 10
    assert caplog.text.count("Redis unavailable") == 1
    assert "SECRET-MARKER" not in caplog.text
    clock.advance(1.0)  # 4 / 4 = 1 token per second
    assert (await limiter.take("k", capacity=40, refill_per_s=4.0)).allowed
    assert not (await limiter.take("k", capacity=40, refill_per_s=4.0)).allowed


@pytest.mark.req("REQ-4.11-4")
async def test_requests_keep_flowing_even_when_the_share_rounds_to_nothing() -> None:
    clock = FakeClock()
    limiter = FallbackRateLimiter(
        Down(), InMemoryRateLimiter(clock), expected_instances=10, clock=clock
    )
    assert (await limiter.take("k", capacity=3, refill_per_s=1.0)).allowed  # at least one token


async def test_redis_is_used_when_it_works() -> None:
    clock = FakeClock()
    primary, local = InMemoryRateLimiter(clock), InMemoryRateLimiter(clock)
    limiter = FallbackRateLimiter(primary, local, expected_instances=2, clock=clock)
    for _ in range(3):
        assert (await limiter.take("k", capacity=3, refill_per_s=1.0)).allowed
    assert not (await limiter.take("k", capacity=3, refill_per_s=1.0)).allowed
    assert (await local.take("local:k", capacity=1, refill_per_s=1.0)).allowed  # untouched


def test_expected_instances_must_be_positive() -> None:
    clock = FakeClock()
    with pytest.raises(ValueError, match="expected_instances"):
        FallbackRateLimiter(Down(), InMemoryRateLimiter(clock), expected_instances=0)


async def test_aclose_closes_the_primary_when_it_can_be_closed() -> None:
    closed: list[bool] = []

    class Closable(Down):
        async def aclose(self) -> None:
            closed.append(True)

    clock = FakeClock()
    await FallbackRateLimiter(Closable(), InMemoryRateLimiter(clock)).aclose()
    await FallbackRateLimiter(Down(), InMemoryRateLimiter(clock)).aclose()
    assert closed == [True]


async def test_the_redis_limiter_repr_hides_the_url() -> None:
    limiter = RedisRateLimiter("redis://user:hunter2@127.0.0.1:1/0")
    assert "hunter2" not in repr(limiter)
    await limiter.aclose()
