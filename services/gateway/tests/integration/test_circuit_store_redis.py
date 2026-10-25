"""``RedisCircuitStore`` passes the shared CircuitStore contract on a real Redis."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.contracts import CircuitStoreContract
from jarvis_core.interfaces import CircuitSnapshot, CircuitState, CircuitStore
from jarvis_gateway.circuit_store import PREFIX, RedisCircuitStore
from redis import asyncio as redis_asyncio

pytestmark = pytest.mark.integration


@pytest.fixture
def redis_url() -> str:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.fail("REDIS_URL must point at a Redis server to run integration tests")
    return url


class TestRedisCircuitStore(CircuitStoreContract):
    @pytest.fixture
    async def store(self, redis_url: str, clock: FakeClock) -> AsyncIterator[CircuitStore]:
        # The contract uses fixed api ids: isolate by namespacing through a fresh prefix per test.
        client = redis_asyncio.from_url(redis_url)
        for key in await client.keys(f"{PREFIX}*"):
            await client.delete(key)
        await client.aclose()
        redis_store = RedisCircuitStore(redis_url, clock=clock)
        yield redis_store
        await redis_store.aclose()


async def test_the_closed_at_field_round_trips(redis_url: str) -> None:
    api = f"it_{uuid.uuid4().hex[:8]}"
    store = RedisCircuitStore(redis_url, clock=FakeClock())
    snap = CircuitSnapshot(CircuitState.CLOSED, closed_at=123.5)
    await store.set(api, snap)
    assert await store.get(api) == snap
    await store.set(api, CircuitSnapshot(CircuitState.HALF_OPEN, 10.0, 60))
    assert await store.get(api) == CircuitSnapshot(CircuitState.HALF_OPEN, 10.0, 60)
    await store.aclose()


async def test_the_repr_hides_the_url() -> None:
    store = RedisCircuitStore("redis://user:hunter2@127.0.0.1:1/0", clock=FakeClock())
    assert "hunter2" not in repr(store)
    await store.aclose()


async def test_listen_delivers_published_changes_and_stops(redis_url: str) -> None:
    import asyncio

    store = RedisCircuitStore(redis_url, clock=FakeClock())
    seen: list[str] = []
    stop = asyncio.Event()

    async def on_change(api_id: str) -> None:
        seen.append(api_id)

    task = asyncio.create_task(store.listen(on_change, stop))
    for _ in range(50):  # wait for the subscription to be live
        await store.publish_change("")  # an empty payload is ignored
        await store.publish_change("api_listen")
        await asyncio.sleep(0.05)
        if seen:
            break
    assert seen
    assert set(seen) == {"api_listen"}
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    await store.aclose()


async def test_listen_retries_when_redis_is_unreachable(caplog: pytest.LogCaptureFixture) -> None:
    import asyncio
    import logging

    store = RedisCircuitStore("redis://127.0.0.1:1/0", clock=FakeClock())
    stop = asyncio.Event()

    async def on_change(api_id: str) -> None:
        raise AssertionError("nothing can arrive")

    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(store.listen(on_change, stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    assert "circuit subscriber lost Redis" in caplog.text
    await store.aclose()
