"""Non-blocking telemetry emission to the Redis Stream ``telemetry`` (SPEC §6.1, §6.3).

``emit`` is a plain synchronous call that never awaits, never does I/O and never raises: it
appends to a bounded queue. One background task sends the queue to Redis in pipelined ``XADD``
batches. A full queue, a failed or hung Redis call, and an event that cannot be encoded all end
the same way: the event is dropped and counted in ``dropped_total``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final, Protocol

from jarvis_core.clock import Clock, SystemClock
from jarvis_core.telemetry import STREAM, RequestEvent, encode
from redis import asyncio as redis_asyncio

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE: Final = 10_000
DEFAULT_BATCH_SIZE: Final = 500
DEFAULT_MAXLEN: Final = 1_000_000
DEFAULT_FLUSH_INTERVAL_S: Final = 0.05
DEFAULT_COMMAND_TIMEOUT_S: Final = 1.0
DEFAULT_CLOSE_TIMEOUT_S: Final = 2.0
_BACKOFF_START_S: Final = 0.1
_BACKOFF_MAX_S: Final = 5.0
_WARN_EVERY_S: Final = 30.0
_FIELD: Final = "event"


class StreamWriter(Protocol):
    async def xadd_batch(self, stream: str, maxlen: int, events: list[str]) -> None:
        """Send all ``events`` in one pipelined round trip with ``MAXLEN ~ maxlen``."""
        ...

    async def aclose(self) -> None: ...


class RedisStreamWriter:
    """``redis.asyncio`` client with short timeouts, separate from the config pub/sub client."""

    def __init__(self, url: str, *, timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S) -> None:
        self._client = redis_asyncio.from_url(
            url, socket_connect_timeout=timeout_s, socket_timeout=timeout_s
        )

    def __repr__(self) -> str:
        return "RedisStreamWriter()"

    async def xadd_batch(self, stream: str, maxlen: int, events: list[str]) -> None:
        pipe = self._client.pipeline(transaction=False)
        for event in events:
            pipe.xadd(stream, {_FIELD: event}, maxlen=maxlen, approximate=True)
        await pipe.execute()

    async def aclose(self) -> None:
        await self._client.aclose()


class NoopTelemetrySink:
    """Telemetry switched off (``JARVIS_TELEMETRY_ENABLED=false``)."""

    dropped_total = 0
    queue_depth = 0

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    def emit(self, event: Mapping[str, Any] | RequestEvent) -> bool:
        return True


class RedisTelemetrySink:
    def __init__(
        self,
        writer: StreamWriter,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        maxlen: int = DEFAULT_MAXLEN,
        stream: str = STREAM,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
        close_timeout_s: float = DEFAULT_CLOSE_TIMEOUT_S,
        clock: Clock | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if queue_size < 1 or batch_size < 1 or maxlen < 1:
            raise ValueError("queue size, batch size and maxlen must be at least 1")
        self._writer = writer
        self._queue_size = queue_size
        self._batch_size = batch_size
        self._maxlen = maxlen
        self._stream = stream
        self._flush_interval_s = flush_interval_s
        self._command_timeout_s = command_timeout_s
        self._close_timeout_s = close_timeout_s
        self._clock = SystemClock() if clock is None else clock
        self._sleep = sleep
        self._queue: deque[Mapping[str, Any] | RequestEvent] = deque()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False
        self._dropped = 0
        self._unlogged = 0
        self._last_warned: float | None = None

    def __repr__(self) -> str:
        return f"RedisTelemetrySink(queue={len(self._queue)}, dropped={self._dropped})"

    @property
    def dropped_total(self) -> int:
        return self._dropped

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    def emit(self, event: Mapping[str, Any] | RequestEvent) -> bool:
        """O(1), no await, no I/O, never raises. False (and counted) if the event is dropped."""
        if self._closing or len(self._queue) >= self._queue_size:
            self._drop(1)
            return False
        self._queue.append(event)
        self._wake.set()
        return True

    def _drop(self, count: int) -> None:
        self._dropped += count
        self._unlogged += count
        now = self._clock.monotonic()
        if self._last_warned is None or now - self._last_warned >= _WARN_EVERY_S:
            logger.warning("telemetry events dropped: %d since the last warning", self._unlogged)
            self._unlogged = 0
            self._last_warned = now

    async def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run(), name="telemetry-flusher")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closing = True
        self._wake.set()
        task, self._task = self._task, None
        try:
            if task is not None:
                try:
                    await asyncio.wait_for(task, self._close_timeout_s)
                except TimeoutError:
                    pass  # wait_for cancelled the flusher
                except Exception:
                    logger.exception("telemetry flusher stopped with an error")
            leftover = len(self._queue)
            self._queue.clear()
            if leftover:
                self._drop(leftover)
        finally:
            self._closed = True
            try:
                await self._writer.aclose()
            except Exception:
                logger.warning("closing the telemetry connection failed")

    def _take(self) -> list[str]:
        lines: list[str] = []
        taken = 0
        while self._queue and taken < self._batch_size:
            item = self._queue.popleft()
            taken += 1
            try:
                if isinstance(item, RequestEvent):
                    lines.append(encode(item))
                else:
                    lines.append(_encode_mapping(item))
            except (TypeError, ValueError):
                self._drop(1)
        return lines

    async def _run(self) -> None:
        backoff = _BACKOFF_START_S
        while True:
            if not self._queue:
                if self._closing:
                    return
                self._wake.clear()
                await self._wake.wait()
                continue
            if len(self._queue) < self._batch_size and not self._closing:
                await self._sleep(self._flush_interval_s)  # let a batch fill
            lines = self._take()
            if not lines:
                continue
            try:
                await asyncio.wait_for(
                    self._writer.xadd_batch(self._stream, self._maxlen, lines),
                    self._command_timeout_s,
                )
                backoff = _BACKOFF_START_S
            except asyncio.CancelledError:
                self._drop(len(lines))  # shutdown gave up on a hung call: these never arrived
                raise
            except Exception:
                # Not retried: a retry would only grow the backlog while Redis is unavailable.
                self._drop(len(lines))
                await self._sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)


def _encode_mapping(event: Mapping[str, Any]) -> str:
    return json.dumps(event, separators=(",", ":"), ensure_ascii=True)
