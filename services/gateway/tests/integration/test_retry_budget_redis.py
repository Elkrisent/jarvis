"""The retry budget against a real Redis: shared window, expiry, and a Redis that is not there."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from jarvis_core.clock import FakeClock
from jarvis_gateway.retry_budget import KEY_PREFIX, RedisBudgetBackend, RetryBudget
from redis import asyncio as redis_asyncio

pytestmark = pytest.mark.integration

CTX = {"project_id": "p1", "request_id": "r1"}
CLOSED_REDIS = "redis://127.0.0.1:1/0"


@pytest.fixture
def redis_url() -> str:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.fail("REDIS_URL must point at a Redis server to run integration tests")
    return url


@pytest.fixture
def api_id() -> str:
    return f"it_{uuid.uuid4().hex[:10]}"


@pytest.fixture
async def cleanup(redis_url: str, api_id: str) -> AsyncIterator[None]:
    yield
    client = redis_asyncio.from_url(redis_url)
    for key in await client.keys(f"{KEY_PREFIX}{api_id}:*"):
        await client.delete(key)
    await client.aclose()


def _budget(url: str, clock: FakeClock) -> RetryBudget:
    return RetryBudget(RedisBudgetBackend(url), clock=clock, flush_interval_s=3600)


@pytest.mark.req("REQ-4.7-7", "REQ-4.7-8")
@pytest.mark.usefixtures("cleanup")
async def test_two_instances_share_one_budget(redis_url: str, api_id: str) -> None:
    clock = FakeClock()
    one, two = _budget(redis_url, clock), _budget(redis_url, clock)
    for _ in range(5):  # 10 requests in total, split across the instances
        one.record_request(api_id)
        two.record_request(api_id)
    granted = 0
    for budget in (one, two, one, two, one, two):
        granted += await budget.try_acquire(api_id, 0.2, **CTX)
    assert granted == 2  # 0.2 * 10, not 2 per instance
    await one.aclose()
    await two.aclose()


@pytest.mark.req("REQ-4.7-7")
@pytest.mark.usefixtures("cleanup")
async def test_the_window_rolls_in_redis(redis_url: str, api_id: str) -> None:
    clock = FakeClock()
    budget = _budget(redis_url, clock)
    for _ in range(5):
        budget.record_request(api_id)
    assert await budget.try_acquire(api_id, 0.2, **CTX) is True
    assert await budget.try_acquire(api_id, 0.2, **CTX) is False
    clock.advance(61)
    assert await budget.try_acquire(api_id, 0.2, **CTX) is False  # the old requests have expired
    for _ in range(5):
        budget.record_request(api_id)
    assert await budget.try_acquire(api_id, 0.2, **CTX) is True
    await budget.aclose()


@pytest.mark.usefixtures("cleanup")
async def test_keys_expire_after_the_window(redis_url: str, api_id: str) -> None:
    budget = _budget(redis_url, FakeClock())
    budget.record_request(api_id)
    await budget.try_acquire(api_id, 0.2, **CTX)
    client = redis_asyncio.from_url(redis_url)
    keys = await client.keys(f"{KEY_PREFIX}{api_id}:*")
    assert keys
    ttls = [await client.ttl(k) for k in keys]
    assert all(0 < ttl <= 61 for ttl in ttls)
    await client.aclose()
    await budget.aclose()


@pytest.mark.req("REQ-4.7-7")
async def test_unreachable_redis_falls_back_to_local_counts(api_id: str) -> None:
    budget = RetryBudget(RedisBudgetBackend(CLOSED_REDIS, timeout_s=0.2), clock=FakeClock())
    for _ in range(5):
        budget.record_request(api_id)
    assert await budget.try_acquire(api_id, 0.2, **CTX) is True
    assert await budget.try_acquire(api_id, 0.2, **CTX) is False
    await budget.aclose()
