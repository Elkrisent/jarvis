"""M2.3 retry storm: an upstream that fails everything must not be hammered by retries.

Two gateway instances share one Redis budget. Needs REDIS_URL (hence the integration marker).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from jarvis_core.memory import InMemoryTelemetrySink
from jarvis_gateway.retry_budget import KEY_PREFIX, RedisBudgetBackend, RetryBudget
from redis import asyncio as redis_asyncio
from rig import GatewayFactory, api_entry
from test_retries import _resp, scripted

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis_url() -> AsyncIterator[str]:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.fail("REDIS_URL must point at a Redis server to run integration tests")
    return url


@pytest.mark.req("REQ-4.7-7", "REQ-4.7-8")
async def test_upstream_calls_per_client_request_stay_within_one_plus_the_budget(
    make_gateway: GatewayFactory, redis_url: str
) -> None:
    api_id = f"storm_{uuid.uuid4().hex[:8]}"
    sink = InMemoryTelemetrySink()
    clients = 200
    ratio = 0.2
    async with scripted(_resp(503)) as upstream:
        gateways = []
        for _ in range(2):
            budget = RetryBudget(
                RedisBudgetBackend(redis_url),
                sink=sink,
                flush_interval_s=0.05,
            )
            entry = api_entry(
                api_id, "none", {"mode": "none"}, upstream.port, retry={"base_ms": 1, "cap_ms": 5}
            )
            gateways.append(make_gateway(apis=[entry], retry_budget=budget))

        async def one(n: int) -> int:
            return (await gateways[n % 2].call("GET", "/gw/none/x")).status_code

        # Waves of 40: the gateway's per-upstream httpx pool holds 100 connections, and this test
        # is about the budget, not about pool exhaustion.
        statuses: list[int] = []
        for wave in range(0, clients, 40):
            statuses += await asyncio.gather(*(one(n) for n in range(wave, wave + 40)))
    redis = redis_asyncio.from_url(redis_url)
    for key in await redis.keys(f"{KEY_PREFIX}{api_id}:*"):
        await redis.delete(key)
    await redis.aclose()

    assert set(statuses) == {503}  # the upstream's own answer, passed through
    calls = len(upstream.heads)
    retries = calls - clients
    assert calls <= clients * (1 + ratio) * 1.1  # flush lag between instances: 10% tolerance
    assert retries >= 1  # the budget was used, not just refused
    events = [e for e in sink.events if isinstance(e, dict)]
    assert events
    assert {e["type"] for e in events} == {"retry_budget_exhausted"}
