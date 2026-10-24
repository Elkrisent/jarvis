"""RedisTelemetrySink against a real Redis (and a Redis that is not there)."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from jarvis_core.telemetry import SCHEMA_VERSION, RequestEvent, encode, event_from_outcome
from jarvis_gateway.telemetry import RedisStreamWriter, RedisTelemetrySink
from redis import asyncio as redis_asyncio

pytestmark = pytest.mark.integration


def make_event(n: int) -> RequestEvent:
    return event_from_outcome(
        request_id=f"r{n}",
        ts="2026-09-24T10:42:01.123Z",
        project_id="prj_1",
        key_id="key_1",
        api_id="api_1",
        method="GET",
        route="/v1/x",
        status=200,
        source="upstream",
        attempts=1,
        latency_ms=3,
        upstream_latency_ms=2,
        req_bytes=0,
        resp_bytes=10,
        error_code=None,
    )


@pytest.fixture
def redis_url() -> str:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.fail("REDIS_URL must point at a Redis server to run integration tests")
    return url


@pytest.fixture
async def stream(redis_url: str) -> AsyncIterator[str]:
    name = f"telemetry_it_{uuid.uuid4().hex[:10]}"
    yield name
    client = redis_asyncio.from_url(redis_url)
    try:
        await client.delete(name)
    finally:
        await client.aclose()


async def wait_for_length(url: str, stream: str, expected: int) -> None:
    client = redis_asyncio.from_url(url)
    try:
        for _ in range(200):
            if await client.xlen(stream) >= expected:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"stream {stream} never reached {expected} entries")
    finally:
        await client.aclose()


@pytest.mark.req("REQ-6.1-1")
async def test_events_land_in_the_stream_with_the_exact_encoding(
    redis_url: str, stream: str
) -> None:
    sink = RedisTelemetrySink(RedisStreamWriter(redis_url), stream=stream, flush_interval_s=0.01)
    await sink.start()
    for n in range(25):
        assert sink.emit(make_event(n))
    await wait_for_length(redis_url, stream, 25)
    await sink.aclose()
    client = redis_asyncio.from_url(redis_url, decode_responses=True)
    try:
        entries = await client.xrange(stream)
    finally:
        await client.aclose()
    assert [fields["event"] for _, fields in entries] == [encode(make_event(n)) for n in range(25)]
    assert all(set(fields) == {"event"} for _, fields in entries)
    assert json.loads(entries[0][1]["event"])["v"] == SCHEMA_VERSION
    assert sink.dropped_total == 0


@pytest.mark.req("REQ-6.1-1")
async def test_the_stream_is_trimmed_approximately_to_maxlen(redis_url: str, stream: str) -> None:
    sink = RedisTelemetrySink(
        RedisStreamWriter(redis_url),
        stream=stream,
        maxlen=100,
        queue_size=5000,
        batch_size=500,
        flush_interval_s=0.0,
    )
    await sink.start()
    for n in range(2000):
        sink.emit(make_event(n))
    for _ in range(200):
        if sink.queue_depth == 0:
            break
        await asyncio.sleep(0.02)
    await sink.aclose()
    client = redis_asyncio.from_url(redis_url)
    try:
        length = await client.xlen(stream)
    finally:
        await client.aclose()
    assert 100 <= length < 2000  # approximate trimming keeps whole macro nodes
    assert sink.dropped_total == 0


@pytest.mark.req("REQ-6.3-2")
@pytest.mark.req("REQ-8-1")
async def test_unreachable_redis_drops_and_counts_without_blocking_emit() -> None:
    sink = RedisTelemetrySink(
        RedisStreamWriter("redis://127.0.0.1:1/0", timeout_s=0.2),
        queue_size=50,
        batch_size=10,
        flush_interval_s=0.0,
    )
    await sink.start()
    started = time.perf_counter()
    for n in range(500):
        sink.emit(make_event(n))
    assert time.perf_counter() - started < 0.2
    for _ in range(100):
        if sink.queue_depth == 0:
            break
        await asyncio.sleep(0.05)
    await sink.aclose()
    assert sink.dropped_total == 500  # nothing could be delivered; every event is counted once


@pytest.mark.req("REQ-6.3-2")
async def test_delivery_resumes_when_redis_comes_back(redis_url: str, stream: str) -> None:
    class Flaky(RedisStreamWriter):
        down = True

        async def xadd_batch(self, stream: str, maxlen: int, events: list[str]) -> None:
            if self.down:
                raise ConnectionError("down")
            await super().xadd_batch(stream, maxlen, events)

    writer = Flaky(redis_url)
    sink = RedisTelemetrySink(writer, stream=stream, flush_interval_s=0.0)
    await sink.start()
    sink.emit(make_event(0))
    for _ in range(100):
        if sink.dropped_total:
            break
        await asyncio.sleep(0.02)
    assert sink.dropped_total == 1
    writer.down = False
    await asyncio.sleep(0.3)  # past the first backoff
    sink.emit(make_event(1))
    await wait_for_length(redis_url, stream, 1)
    await sink.aclose()
    assert sink.dropped_total == 1
