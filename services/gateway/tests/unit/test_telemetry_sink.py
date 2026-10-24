"""RedisTelemetrySink with a fake stream writer: bounded, non-blocking, drops are counted."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping

import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.telemetry import RequestEvent, encode, event_from_outcome
from jarvis_gateway.telemetry import NoopTelemetrySink, RedisStreamWriter, RedisTelemetrySink

pytestmark = pytest.mark.unit

LOGGER = "jarvis_gateway.telemetry"


def make_event(n: int = 0) -> RequestEvent:
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


class FakeWriter:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.calls: list[tuple[str, int]] = []
        self.fail = False
        self.hang = False
        self.closed = False

    async def xadd_batch(self, stream: str, maxlen: int, events: list[str]) -> None:
        self.calls.append((stream, maxlen))
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise OSError("redis is down")
        self.batches.append(list(events))

    async def aclose(self) -> None:
        self.closed = True


async def settle(sink: RedisTelemetrySink, writer: FakeWriter, expected: int) -> None:
    for _ in range(500):
        if sum(len(b) for b in writer.batches) >= expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"only {sum(len(b) for b in writer.batches)} of {expected} flushed")


def make_sink(writer: FakeWriter, **kwargs: object) -> RedisTelemetrySink:
    clock = FakeClock()
    options: dict[str, object] = {
        "queue_size": 10,
        "batch_size": 4,
        "flush_interval_s": 0.0,
        "command_timeout_s": 0.05,
        "clock": clock,
        "sleep": lambda _s: asyncio.sleep(0),
    }
    options.update(kwargs)
    return RedisTelemetrySink(writer, **options)  # type: ignore[arg-type]


@pytest.mark.req("REQ-6.3-2")
async def test_full_queue_drops_the_newest_and_counts_each_drop() -> None:
    writer = FakeWriter()
    sink = make_sink(writer, queue_size=3)  # not started: nothing drains
    results = [sink.emit(make_event(n)) for n in range(5)]
    assert results == [True, True, True, False, False]
    assert sink.dropped_total == 2
    assert sink.queue_depth == 3
    await sink.start()
    await settle(sink, writer, 3)
    await sink.aclose()
    sent = [line for batch in writer.batches for line in batch]
    assert sent == [encode(make_event(n)) for n in range(3)]  # oldest kept, in order


@pytest.mark.req("REQ-6.3-1")
async def test_events_go_out_in_batches_of_at_most_batch_size() -> None:
    writer = FakeWriter()
    sink = make_sink(writer, queue_size=100, batch_size=4)
    for n in range(10):
        assert sink.emit(make_event(n))
    await sink.start()
    await settle(sink, writer, 10)
    await sink.aclose()
    assert [len(b) for b in writer.batches] == [4, 4, 2]
    assert set(writer.calls) == {("telemetry", 1_000_000)}
    assert sink.dropped_total == 0


@pytest.mark.req("REQ-6.3-4")
async def test_emit_is_a_plain_synchronous_call_that_never_raises() -> None:
    writer = FakeWriter()
    sink = make_sink(writer)
    assert sink.emit(make_event(1)) is True  # before start: queued
    assert sink.emit({"anything": object()}) is True  # unserialisable: dropped later, not here
    await sink.start()
    await sink.aclose()
    assert sink.emit(make_event(2)) is False  # after close
    assert sink.dropped_total >= 1


async def test_unserialisable_event_is_dropped_and_counted_by_the_flusher() -> None:
    writer = FakeWriter()
    sink = make_sink(writer)
    sink.emit({"bad": object()})
    sink.emit(make_event(1))
    await sink.start()
    await settle(sink, writer, 1)
    await sink.aclose()
    assert sink.dropped_total == 1
    assert writer.batches == [[encode(make_event(1))]]


@pytest.mark.req("REQ-6.3-2")
async def test_redis_error_drops_that_batch_and_later_batches_recover() -> None:
    writer = FakeWriter()
    writer.fail = True
    sink = make_sink(writer, batch_size=3)
    await sink.start()
    for n in range(3):
        sink.emit(make_event(n))
    for _ in range(50):
        await asyncio.sleep(0)
    assert sink.dropped_total == 3
    writer.fail = False
    sink.emit(make_event(9))
    await settle(sink, writer, 1)
    await sink.aclose()
    assert sink.dropped_total == 3
    assert writer.batches == [[encode(make_event(9))]]


async def test_backoff_after_failures_is_capped() -> None:
    writer = FakeWriter()
    writer.fail = True
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)
        await asyncio.sleep(0)

    sink = make_sink(writer, batch_size=1, sleep=record, flush_interval_s=0.0)
    await sink.start()
    for n in range(12):
        sink.emit(make_event(n))
        for _ in range(5):
            await asyncio.sleep(0)
    await sink.aclose()
    backoffs = [w for w in waits if w > 0]
    assert backoffs[0] == pytest.approx(0.1)
    assert max(backoffs) == pytest.approx(5.0)
    assert backoffs == sorted(backoffs)


@pytest.mark.req("REQ-6.3-4")
async def test_hung_redis_is_cut_at_the_command_timeout_and_emit_stays_fast() -> None:
    writer = FakeWriter()
    writer.hang = True
    sink = make_sink(writer, queue_size=20_000, command_timeout_s=0.05)
    await sink.start()
    sink.emit(make_event(0))
    await asyncio.sleep(0.01)  # the flusher is now stuck inside the hung call
    started = time.perf_counter()
    for n in range(10_000):
        sink.emit(make_event(n))
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5  # a loop of plain synchronous calls: nothing waits on Redis
    await asyncio.sleep(0.1)  # past the command timeout
    assert sink.dropped_total >= 1  # the stuck batch was cut and counted
    await sink.aclose()


async def test_close_flushes_what_is_queued_and_counts_leftovers() -> None:
    writer = FakeWriter()
    sink = make_sink(writer, queue_size=100)
    for n in range(6):
        sink.emit(make_event(n))
    await sink.start()
    await sink.aclose()
    assert sum(len(b) for b in writer.batches) + sink.dropped_total == 6
    assert writer.closed
    await sink.aclose()  # idempotent


async def test_close_gives_up_on_a_hung_redis_and_counts_the_rest() -> None:
    writer = FakeWriter()
    writer.hang = True
    sink = make_sink(writer, queue_size=100, command_timeout_s=10.0, close_timeout_s=0.05)
    for n in range(9):
        sink.emit(make_event(n))
    await sink.start()
    await asyncio.sleep(0.01)
    await sink.aclose()
    assert sink.dropped_total == 9
    assert writer.closed


async def test_drop_warning_is_rate_limited_and_carries_counts_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer = FakeWriter()
    clock = FakeClock()
    sink = make_sink(writer, queue_size=1, clock=clock)
    sink.emit(make_event(0))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for n in range(1, 50):
            sink.emit(make_event(n))
        assert len([r for r in caplog.records if r.name == LOGGER]) == 1
        clock.advance(31)
        sink.emit(make_event(99))
        records = [r for r in caplog.records if r.name == LOGGER]
        assert len(records) == 2
    text = " ".join(r.getMessage() for r in records)
    assert "r1" not in text
    assert "/v1/x" not in text
    assert "dropped" in text


async def test_sink_repr_has_no_url() -> None:
    assert "://" not in repr(make_sink(FakeWriter()))
    assert "://" not in repr(RedisStreamWriter("redis://user:pw@host:6379/0"))


def test_noop_sink_accepts_everything_and_counts_nothing() -> None:
    sink = NoopTelemetrySink()
    assert sink.emit(make_event(1)) is True
    assert sink.dropped_total == 0
    assert sink.queue_depth == 0


def test_emit_signature_accepts_mappings() -> None:
    sink = NoopTelemetrySink()
    mapping: Mapping[str, object] = {"a": 1}
    assert sink.emit(mapping) is True
