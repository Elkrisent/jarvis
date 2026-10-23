"""parse_event and the Redis subscribe loop, with a fake pub/sub connection and a fake clock."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
from jarvis_core.clock import FakeClock
from jarvis_gateway.config import events
from jarvis_gateway.config.events import (
    CHANNELS,
    ApiChanged,
    ConfigEvent,
    Invalidated,
    parse_event,
    subscribe,
)
from redis.exceptions import ConnectionError as RedisConnectionError

pytestmark = pytest.mark.unit

LOGGER = "jarvis_gateway.config.events"


def test_channels() -> None:
    assert CHANNELS == ("config:changed", "config:invalidated")


@pytest.mark.parametrize("payload", [b'{"api_id":"a1","version":4}', '{"api_id":"a1","version":4}'])
def test_parse_api_changed_bytes_and_str(payload: bytes | str) -> None:
    assert parse_event("config:changed", payload) == ApiChanged("a1", 4)


@pytest.mark.parametrize("kind", ["api", "key", "project"])
def test_parse_invalidated_every_kind(kind: str) -> None:
    payload = f'{{"kind":"{kind}","id":"x9"}}'
    assert parse_event("config:invalidated", payload) == Invalidated(kind, "x9")  # type: ignore[arg-type]
    assert parse_event("config:invalidated", payload.encode()) == Invalidated(kind, "x9")  # type: ignore[arg-type]


def test_parse_accepts_a_bytes_channel() -> None:
    assert parse_event(b"config:changed", b'{"api_id":"a","version":1}') == ApiChanged("a", 1)


def test_parse_ignores_extra_fields() -> None:
    assert parse_event("config:changed", '{"api_id":"a","version":2,"x":1}') == ApiChanged("a", 2)


@pytest.mark.parametrize(
    ("channel", "payload"),
    [
        ("config:changed", "not json"),
        ("config:changed", b"\xff\xfe"),
        ("config:changed", ""),
        ("config:changed", "[1,2]"),
        ("config:changed", '"a string"'),
        ("config:changed", "null"),
        ("config:changed", '{"api_id":"a"}'),
        ("config:changed", '{"version":3}'),
        ("config:changed", '{"api_id":3,"version":3}'),
        ("config:changed", '{"api_id":"","version":3}'),
        ("config:changed", '{"api_id":"a","version":"3"}'),
        ("config:changed", '{"api_id":"a","version":true}'),
        ("config:changed", '{"api_id":"a","version":0}'),
        ("config:changed", '{"api_id":"a","version":-1}'),
        ("config:changed", '{"api_id":"a","version":3.0}'),
        ("config:changed", '{"api_id":"a","version":NaN}'),
        ("config:invalidated", '{"kind":"user","id":"x"}'),
        ("config:invalidated", '{"kind":"API","id":"x"}'),
        ("config:invalidated", '{"kind":"api"}'),
        ("config:invalidated", '{"kind":"api","id":5}'),
        ("config:invalidated", '{"kind":"api","id":""}'),
        ("config:invalidated", '{"kind":null,"id":"x"}'),
        ("config:invalidated", '{"api_id":"a","version":3}'),
        ("config:other", '{"api_id":"a","version":3}'),
        ("config:changed:extra", '{"api_id":"a","version":3}'),
        ("", '{"kind":"api","id":"x"}'),
    ],
)
def test_parse_rejects_malformed(channel: str, payload: bytes | str) -> None:
    assert parse_event(channel, payload) is None


def test_parse_rejects_oversized_payload() -> None:
    big = '{"api_id":"' + "a" * 5000 + '","version":1}'
    assert parse_event("config:changed", big) is None


def test_parse_rejects_non_text_payload() -> None:
    assert parse_event("config:changed", 42) is None  # type: ignore[arg-type]


def test_parse_rejects_deeply_nested_json() -> None:
    assert parse_event("config:changed", "[" * 100_000 + "]" * 100_000) is None


# --- subscribe -------------------------------------------------------------------------------


def _msg(channel: str, data: bytes | str, kind: str = "message") -> dict[str, Any]:
    return {"type": kind, "channel": channel.encode(), "data": data, "pattern": None}


class FakeConn:
    """A pub/sub connection: serves ``script`` then raises ``end`` (or idles until stopped)."""

    def __init__(
        self,
        script: list[dict[str, Any] | Callable[[], None]] | None = None,
        *,
        fail_subscribe: BaseException | None = None,
        end: BaseException | None = None,
        confirm: bool = True,
    ) -> None:
        self.script = list(script or [])
        self.fail_subscribe = fail_subscribe
        self.end = end
        self.confirm = confirm
        self.subscribed: tuple[str, ...] = ()
        self.closed = False
        self.queue: list[dict[str, Any] | Callable[[], None]] = []

    async def subscribe(self, *channels: str) -> None:
        if self.fail_subscribe is not None:
            raise self.fail_subscribe
        self.subscribed = channels
        if self.confirm:
            self.queue = [_msg(c, b"1", "subscribe") for c in channels]
        self.queue += self.script

    async def get_message(self, timeout: float) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        while self.queue:
            item = self.queue.pop(0)
            if callable(item):
                item()
                continue
            return item
        if self.end is not None:
            raise self.end
        return None

    async def aclose(self) -> None:
        self.closed = True


class Factory:
    def __init__(self, conns: list[FakeConn], clock: FakeClock, stop: asyncio.Event) -> None:
        self.conns = conns
        self.clock = clock
        self.stop = stop
        self.attempts: list[float] = []
        self.made: list[FakeConn] = []

    def __call__(self, url: str) -> FakeConn:
        self.attempts.append(self.clock.monotonic())
        if not self.conns:
            self.stop.set()
            return FakeConn(fail_subscribe=RedisConnectionError("gone"))
        conn = self.conns.pop(0)
        self.made.append(conn)
        return conn


class Recorder:
    def __init__(self) -> None:
        self.log: list[str | ConfigEvent] = []

    async def on_event(self, event: ConfigEvent) -> None:
        self.log.append(event)

    async def on_connected(self) -> None:
        self.log.append("connected")


async def _run(factory: Factory, rec: Recorder, clock: FakeClock, stop: asyncio.Event) -> None:
    await asyncio.wait_for(
        subscribe("redis://x", rec.on_event, rec.on_connected, clock, stop, connect=factory),
        timeout=5,
    )


async def test_subscribe_dispatches_events_after_on_connected() -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()
    conn = FakeConn(
        [
            _msg("config:changed", b'{"api_id":"a","version":2}'),
            _msg("config:invalidated", '{"kind":"key","id":"k"}'),
            stop.set,
        ]
    )
    await _run(Factory([conn], clock, stop), rec, clock, stop)
    assert conn.subscribed == CHANNELS
    assert rec.log == ["connected", ApiChanged("a", 2), Invalidated("key", "k")]
    assert conn.closed


async def test_on_connected_waits_for_both_subscribe_confirmations() -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()
    conn = FakeConn(confirm=False)

    def confirm_one() -> None:
        assert rec.log == []
        conn.queue.append(_msg("config:changed", b"1", "subscribe"))
        conn.queue.append(_msg("config:changed", b'{"api_id":"a","version":1}'))
        conn.queue.append(confirm_two)

    def confirm_two() -> None:
        assert rec.log == [ApiChanged("a", 1)]
        conn.queue.append(_msg("config:invalidated", b"2", "subscribe"))
        conn.queue.append(stop.set)

    conn.script = [confirm_one]
    await _run(Factory([conn], clock, stop), rec, clock, stop)
    assert rec.log == [ApiChanged("a", 1), "connected"]


async def test_missing_confirmation_counts_as_a_failed_connect() -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()
    silent = FakeConn(confirm=False)  # never confirms: get_message keeps returning None
    factory = Factory([silent], clock, stop)
    await _run(factory, rec, clock, stop)
    assert rec.log == []
    assert silent.closed
    assert len(factory.attempts) == 2


async def test_malformed_payloads_are_dropped_with_a_warning_naming_the_channel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()
    conn = FakeConn(
        [
            _msg("config:changed", b'{"api_id":"SECRET-MARKER","version":"x"}'),
            _msg("config:invalidated", b"garbage SECRET-MARKER"),
            _msg("config:changed", b"1", "unsubscribe"),
            _msg("config:changed", b'{"api_id":"ok","version":1}'),
            stop.set,
        ]
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _run(Factory([conn], clock, stop), rec, clock, stop)
    assert rec.log == ["connected", ApiChanged("ok", 1)]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "config:changed" in warnings[0]
    assert "config:invalidated" in warnings[1]
    assert "SECRET-MARKER" not in caplog.text


async def test_unknown_channel_is_not_echoed(caplog: pytest.LogCaptureFixture) -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()
    conn = FakeConn([_msg("evil:SECRET-MARKER", b"{}"), stop.set])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _run(Factory([conn], clock, stop), rec, clock, stop)
    assert rec.log == ["connected"]
    assert "SECRET-MARKER" not in caplog.text
    assert "unexpected channel" in caplog.text


async def test_on_connected_runs_after_every_reconnect() -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()
    first = FakeConn([_msg("config:changed", b'{"api_id":"a","version":1}')], end=OSError("reset"))
    second = FakeConn([_msg("config:changed", b'{"api_id":"a","version":2}'), stop.set])
    factory = Factory([first, second], clock, stop)
    await _run(factory, rec, clock, stop)
    assert rec.log == ["connected", ApiChanged("a", 1), "connected", ApiChanged("a", 2)]
    assert first.closed
    assert second.closed


async def test_backoff_doubles_to_the_cap_on_repeated_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()
    failing = [FakeConn(fail_subscribe=RedisConnectionError("refused")) for _ in range(8)]
    factory = Factory(failing, clock, stop)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _run(factory, rec, clock, stop)
    gaps = [round(b - a, 6) for a, b in zip(factory.attempts, factory.attempts[1:], strict=False)]
    assert gaps == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0, 5.0]
    assert rec.log == []
    assert all(c.closed for c in factory.made)
    assert "redis://x" not in caplog.text


async def test_backoff_resets_after_a_connection_that_stayed_up() -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()
    conns = [
        FakeConn(fail_subscribe=OSError()),
        FakeConn(fail_subscribe=OSError()),
        FakeConn(fail_subscribe=OSError()),
        FakeConn([lambda: clock.advance(60)], end=RedisConnectionError("dropped")),
        FakeConn(fail_subscribe=OSError()),
    ]
    factory = Factory(conns, clock, stop)
    await _run(factory, rec, clock, stop)
    gaps = [round(b - a, 6) for a, b in zip(factory.attempts, factory.attempts[1:], strict=False)]
    # 0.5, 1, 2 while failing; the healthy connection lasted 60 s; then the backoff starts over.
    assert gaps == [0.5, 1.0, 2.0, 60.5, 1.0]


async def test_a_short_lived_connection_keeps_doubling() -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()
    conns = [FakeConn(end=OSError()) for _ in range(4)]
    factory = Factory(conns, clock, stop)
    await _run(factory, rec, clock, stop)
    gaps = [round(b - a, 6) for a, b in zip(factory.attempts, factory.attempts[1:], strict=False)]
    assert gaps == [0.5, 1.0, 2.0, 4.0]
    assert rec.log.count("connected") == 4


async def test_stop_ends_the_loop_while_idle() -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()
    conn = FakeConn()  # confirms, then idles (get_message returns None)
    task = asyncio.create_task(
        subscribe("redis://x", rec.on_event, rec.on_connected, clock, stop, connect=lambda u: conn)
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert rec.log == ["connected"]
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert conn.closed


async def test_stop_cuts_the_backoff_wait_short() -> None:
    stop, rec = asyncio.Event(), Recorder()

    class SlowClock(FakeClock):
        async def sleep(self, seconds: float) -> None:
            await asyncio.Event().wait()  # never wakes on its own

    clock = SlowClock()
    attempts: list[int] = []

    def connect(url: str) -> FakeConn:
        attempts.append(1)
        return FakeConn(fail_subscribe=OSError())

    task = asyncio.create_task(
        subscribe("redis://x", rec.on_event, rec.on_connected, clock, stop, connect=connect)
    )
    for _ in range(20):
        await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert attempts == [1]


async def test_a_stop_already_set_returns_at_once() -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()
    stop.set()
    factory = Factory([FakeConn()], clock, stop)
    await _run(factory, rec, clock, stop)
    assert factory.attempts == []


async def test_errors_never_escape(caplog: pytest.LogCaptureFixture) -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()

    def broken_factory(url: str) -> FakeConn:
        if len(attempts) == 3:
            stop.set()
        attempts.append(1)
        raise ValueError("bad url SECRET-MARKER")

    attempts: list[int] = []
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await asyncio.wait_for(
            subscribe(
                "redis://x", rec.on_event, rec.on_connected, clock, stop, connect=broken_factory
            ),
            timeout=5,
        )
    assert len(attempts) == 4
    assert "SECRET-MARKER" not in caplog.text
    assert "ValueError" in caplog.text


async def test_a_failing_close_is_ignored() -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()

    class BadClose(FakeConn):
        async def aclose(self) -> None:
            raise RedisConnectionError("already gone")

    conn = BadClose([stop.set])
    await _run(Factory([conn], clock, stop), rec, clock, stop)
    assert rec.log == ["connected"]


async def test_callback_errors_are_logged_and_the_loop_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, stop = FakeClock(), asyncio.Event()
    seen: list[ConfigEvent] = []

    async def on_event(event: ConfigEvent) -> None:
        seen.append(event)
        raise RuntimeError("handler SECRET-MARKER")

    async def on_connected() -> None:
        raise RuntimeError("resync SECRET-MARKER")

    conn = FakeConn(
        [
            _msg("config:changed", b'{"api_id":"a","version":1}'),
            _msg("config:changed", b'{"api_id":"a","version":2}'),
            stop.set,
        ]
    )
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await asyncio.wait_for(
            subscribe("redis://x", on_event, on_connected, clock, stop, connect=lambda u: conn),
            timeout=5,
        )
    assert seen == [ApiChanged("a", 1), ApiChanged("a", 2)]
    assert "SECRET-MARKER" not in caplog.text
    assert caplog.text.count("RuntimeError") == 3


async def test_cancellation_propagates_and_closes_the_connection() -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()
    conn = FakeConn()
    task = asyncio.create_task(
        subscribe("redis://x", rec.on_event, rec.on_connected, clock, stop, connect=lambda u: conn)
    )
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn.closed


async def test_default_connect_wraps_redis_pubsub(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real factory configures redis-py with the decided health check and closes both."""
    made: dict[str, Any] = {}

    class FakePubSub:
        def __init__(self) -> None:
            self.closed = False
            self.channels: tuple[str, ...] = ()

        async def subscribe(self, *channels: str) -> None:
            self.channels = channels

        async def get_message(
            self, *, ignore_subscribe_messages: bool, timeout: float
        ) -> dict[str, Any] | None:
            made["get"] = (ignore_subscribe_messages, timeout)
            return None

        async def aclose(self) -> None:
            self.closed = True

    class FakeRedis:
        def __init__(self) -> None:
            self.ps = FakePubSub()
            self.closed = False

        def pubsub(self) -> FakePubSub:
            return self.ps

        async def aclose(self) -> None:
            self.closed = True

    def from_url(url: str, **kwargs: Any) -> FakeRedis:
        made["url"], made["kwargs"] = url, kwargs
        made["client"] = FakeRedis()
        return made["client"]

    monkeypatch.setattr(events.redis_asyncio, "from_url", from_url)
    conn = events.connect_redis("redis://h:1/0")
    await conn.subscribe(*CHANNELS)
    assert await conn.get_message(timeout=0.5) is None
    await conn.aclose()
    assert made["url"] == "redis://h:1/0"
    assert made["kwargs"]["health_check_interval"] == 5
    assert made["get"] == (False, 0.5)
    assert made["client"].ps.channels == CHANNELS
    assert made["client"].ps.closed
    assert made["client"].closed
    assert "h:1" not in repr(conn)


async def test_str_channels_from_the_connection_are_understood() -> None:
    clock, stop, rec = FakeClock(), asyncio.Event(), Recorder()
    message = {"type": "message", "channel": "config:changed", "data": '{"api_id":"a","version":1}'}
    conn = FakeConn([message, stop.set])
    await _run(Factory([conn], clock, stop), rec, clock, stop)
    assert rec.log == ["connected", ApiChanged("a", 1)]


async def test_cancellation_inside_a_callback_propagates() -> None:
    clock, stop = FakeClock(), asyncio.Event()
    entered = asyncio.Event()

    async def on_event(event: ConfigEvent) -> None:
        entered.set()
        await asyncio.Event().wait()

    async def on_connected() -> None:
        return None

    conn = FakeConn([_msg("config:changed", b'{"api_id":"a","version":1}')])
    task = asyncio.create_task(
        subscribe("redis://x", on_event, on_connected, clock, stop, connect=lambda u: conn)
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn.closed
