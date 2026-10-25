"""Is Redis usable right now? A breaker around the gateway's Redis calls (SPEC D3, M2.7).

Every Redis-backed helper (rate limiter, circuit store, retry budget) already falls back to local
state when a call fails. That alone leaves each request waiting for the call's timeout while Redis
is down or black-holed. This tracks the outcome of those calls: after a few failures in a row
Redis counts as down and the helpers skip it at once (no I/O, no waiting) until a background PING
succeeds. The first request after recovery uses Redis again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from jarvis_core.clock import Clock, SystemClock
from jarvis_core.interfaces import TelemetrySink
from jarvis_core.telemetry import SCHEMA_VERSION, format_ts
from redis import asyncio as redis_asyncio
from redis import exceptions as redis_exceptions

log = logging.getLogger(__name__)


class RedisUnavailable(Exception):
    """Redis is known to be down: nothing was attempted."""


Ping = Callable[[], Awaitable[None]]


class RedisHealth:
    def __init__(
        self,
        ping: Ping,
        *,
        clock: Clock | None = None,
        sink: TelemetrySink | None = None,
        failure_threshold: int = 3,
        probe_interval_s: float = 0.5,
        ping_timeout_s: float = 0.5,
        closer: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self._ping = ping
        self._clock = SystemClock() if clock is None else clock
        self._sink = sink
        self._threshold = failure_threshold
        self._probe_interval_s = probe_interval_s
        self._ping_timeout_s = ping_timeout_s
        self._closer = closer
        self._failures = 0
        self._down_since: float | None = None
        self._probe: asyncio.Task[None] | None = None
        self._closed = False

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisHealth:
        client = redis_asyncio.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5)

        async def ping() -> None:
            await client.ping()

        return cls(ping, closer=client.aclose, **kwargs)

    def __repr__(self) -> str:
        return f"RedisHealth(available={self.available})"

    @property
    def available(self) -> bool:
        return self._down_since is None

    @property
    def down_since(self) -> float | None:
        return self._down_since

    async def call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run one Redis operation, or raise ``RedisUnavailable`` at once if Redis is known down."""
        if self._down_since is not None:
            raise RedisUnavailable
        try:
            result = await operation()
        except Exception as exc:
            # A timeout means Redis may be black-holed, where every further call would also wait
            # out its timeout: that counts as down at once. Other errors (refused, reset) are
            # fast, so a few in a row are needed.
            self.record_failure(
                severe=isinstance(exc, TimeoutError | redis_exceptions.TimeoutError)
            )
            raise
        self._failures = 0
        return result

    def record_failure(self, *, severe: bool = False) -> None:
        if self._down_since is not None or self._closed:
            return
        self._failures += 1
        if severe or self._failures >= self._threshold:
            self._down_since = self._clock.now()
            log.warning("Redis is unavailable: the gateway now runs on local state")
            self._probe = asyncio.get_running_loop().create_task(self._probe_loop())

    async def aclose(self) -> None:
        self._closed = True
        task, self._probe = self._probe, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._closer is not None:
            await self._closer()

    async def _probe_loop(self) -> None:
        while True:
            await asyncio.sleep(self._probe_interval_s)
            try:
                async with asyncio.timeout(self._ping_timeout_s):
                    await self._ping()
            except Exception as exc:
                log.debug("Redis still unavailable (%s)", type(exc).__name__)
                continue
            self._recover()
            return

    def _recover(self) -> None:
        down_since = self._down_since
        self._down_since = None
        self._failures = 0
        self._probe = None
        now = self._clock.now()
        started = now if down_since is None else down_since
        log.warning("Redis is back after %.1f s: shared state is in use again", now - started)
        if self._sink is not None:
            self._sink.emit(
                {
                    "v": SCHEMA_VERSION,
                    "kind": "event",
                    "type": "redis_unavailable",
                    "ts": format_ts(now),
                    "down_since": format_ts(started),
                    "duration_s": round(now - started, 3),
                }
            )
