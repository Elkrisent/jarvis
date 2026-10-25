"""``RedisRateLimiter`` passes the shared RateLimiter contract, and never over-admits."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.contracts import RateLimiterContract
from jarvis_core.interfaces import RateLimiter
from jarvis_gateway.rate_limit import PREFIX, RedisRateLimiter
from redis import asyncio as redis_asyncio

pytestmark = pytest.mark.integration


@pytest.fixture
def redis_url() -> str:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.fail("REDIS_URL must point at a Redis server to run integration tests")
    return url


async def _wipe(url: str) -> None:
    client = redis_asyncio.from_url(url)
    for key in await client.keys(f"{PREFIX}*"):
        await client.delete(key)
    await client.aclose()


class TestRedisRateLimiter(RateLimiterContract):
    @pytest.fixture
    async def limiter(self, redis_url: str, clock: FakeClock) -> AsyncIterator[RateLimiter]:
        await _wipe(redis_url)
        limiter = RedisRateLimiter(redis_url, clock=clock)
        yield limiter
        await limiter.aclose()


@pytest.mark.req("REQ-4.11-1")
async def test_hundreds_of_simultaneous_takes_never_over_admit(redis_url: str) -> None:
    key = f"it_{uuid.uuid4().hex[:8]}"
    # Frozen time: no refill at all. A long timeout: this test is about admission, not latency.
    limiter = RedisRateLimiter(redis_url, clock=FakeClock(), timeout_s=10.0)
    results = await asyncio.gather(
        *(limiter.take(key, capacity=100, refill_per_s=1.0) for _ in range(600))
    )
    assert sum(r.allowed for r in results) == 100
    await limiter.aclose()


@pytest.mark.req("REQ-4.11-1")
async def test_three_instances_share_one_bucket(redis_url: str) -> None:
    key = f"it_{uuid.uuid4().hex[:8]}"
    clock = FakeClock()
    instances = [RedisRateLimiter(redis_url, clock=clock, timeout_s=10.0) for _ in range(3)]
    results = await asyncio.gather(
        *(instances[n % 3].take(key, capacity=50, refill_per_s=1.0) for n in range(300))
    )
    assert sum(r.allowed for r in results) == 50  # not 50 each
    clock.advance(10)  # ten seconds of refill: ten more tokens, no more
    more = await asyncio.gather(
        *(instances[n % 3].take(key, capacity=50, refill_per_s=1.0) for n in range(100))
    )
    assert sum(r.allowed for r in more) == 10
    for instance in instances:
        await instance.aclose()


@pytest.mark.req("REQ-4.11-6")
async def test_a_rejected_take_does_not_consume_tokens(redis_url: str) -> None:
    key = f"it_{uuid.uuid4().hex[:8]}"
    clock = FakeClock()
    limiter = RedisRateLimiter(redis_url, clock=clock)
    assert (await limiter.take(key, capacity=2, refill_per_s=1.0, cost=2)).allowed
    for _ in range(5):
        assert not (await limiter.take(key, capacity=2, refill_per_s=1.0)).allowed
    clock.advance(1.0)  # exactly one token back, not reduced by the rejected attempts
    assert (await limiter.take(key, capacity=2, refill_per_s=1.0)).allowed
    assert not (await limiter.take(key, capacity=2, refill_per_s=1.0)).allowed
    await limiter.aclose()


async def test_keys_expire_and_the_repr_hides_the_url(redis_url: str) -> None:
    key = f"it_{uuid.uuid4().hex[:8]}"
    limiter = RedisRateLimiter(redis_url, clock=FakeClock())
    await limiter.take(key, capacity=10, refill_per_s=5.0)
    client = redis_asyncio.from_url(redis_url)
    assert 0 < await client.ttl(f"{PREFIX}{key}") <= 3  # ceil(10 / 5) + 1
    await client.delete(f"{PREFIX}{key}")
    await client.aclose()
    await limiter.aclose()
    assert "redis" not in repr(RedisRateLimiter("redis://user:hunter2@h:1/0"))


async def test_an_instance_whose_clock_runs_behind_does_not_drain_the_bucket(
    redis_url: str,
) -> None:
    key = f"it_{uuid.uuid4().hex[:8]}"
    ahead, behind = FakeClock(start=1_000_000.0), FakeClock(start=999_990.0)
    first = RedisRateLimiter(redis_url, clock=ahead)
    second = RedisRateLimiter(redis_url, clock=behind)
    assert (await first.take(key, capacity=5, refill_per_s=1.0)).allowed
    # Ten seconds "in the past": elapsed time is clamped at zero, so tokens stay at four.
    results = [await second.take(key, capacity=5, refill_per_s=1.0) for _ in range(4)]
    assert all(r.allowed for r in results)
    assert not (await second.take(key, capacity=5, refill_per_s=1.0)).allowed
    await first.aclose()
    await second.aclose()
