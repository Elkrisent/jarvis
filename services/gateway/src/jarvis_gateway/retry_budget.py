"""The per-API retry budget (SPEC §4.7): retries may not exceed ``budget_ratio`` of requests over
a rolling 60 s window, shared by every gateway instance through Redis.

``record_request`` is the hot path: no await, no I/O. The count is flushed to Redis once a second
and whenever a retry asks for permission, so the shared figure lags by at most the flush interval.
With Redis unreachable each instance falls back to the same rule over its own counts (SPEC D3:
looser protection, requests keep flowing).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Protocol

from jarvis_core.clock import Clock, SystemClock
from jarvis_core.interfaces import TelemetrySink
from jarvis_core.telemetry import SCHEMA_VERSION, format_ts
from redis import asyncio as redis_asyncio

from jarvis_gateway.redis_health import RedisHealth, RedisUnavailable

log = logging.getLogger(__name__)

WINDOW_S = 60
KEY_PREFIX = "jarvis:rb:"
_EPSILON = 1e-9

# Per second: a hash {r: requests, t: retries}. Sum the window, then take a retry if it still fits.
_ACQUIRE = """
local prefix, now, pending = ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3])
local ratio, window = tonumber(ARGV[4]), tonumber(ARGV[5])
local current = prefix .. now
if pending > 0 then
  redis.call('HINCRBY', current, 'r', pending)
  redis.call('EXPIRE', current, window + 1)
end
local requests, retries = 0, 0
for s = now - window + 1, now do
  local v = redis.call('HMGET', prefix .. s, 'r', 't')
  requests = requests + (tonumber(v[1]) or 0)
  retries = retries + (tonumber(v[2]) or 0)
end
if retries + 1 <= ratio * requests + 1e-9 then
  redis.call('HINCRBY', current, 't', 1)
  redis.call('EXPIRE', current, window + 1)
  return 1
end
return 0
"""


class BudgetBackend(Protocol):
    async def add_requests(self, api_id: str, now_s: int, count: int) -> None: ...

    async def acquire(self, api_id: str, now_s: int, pending: int, ratio: float) -> bool: ...

    async def aclose(self) -> None: ...


class RedisBudgetBackend:
    def __init__(self, url: str, *, timeout_s: float = 0.5) -> None:
        self._client = redis_asyncio.from_url(
            url, socket_connect_timeout=timeout_s, socket_timeout=timeout_s
        )
        self._script = self._client.register_script(_ACQUIRE)

    def __repr__(self) -> str:
        return "RedisBudgetBackend()"

    async def add_requests(self, api_id: str, now_s: int, count: int) -> None:
        key = f"{KEY_PREFIX}{api_id}:{now_s}"
        pipe = self._client.pipeline(transaction=False)
        pipe.hincrby(key, "r", count)
        pipe.expire(key, WINDOW_S + 1)
        await pipe.execute()

    async def acquire(self, api_id: str, now_s: int, pending: int, ratio: float) -> bool:
        result: Any = await self._script(
            args=[f"{KEY_PREFIX}{api_id}:", now_s, pending, repr(ratio), WINDOW_S]
        )
        return bool(result)

    async def aclose(self) -> None:
        await self._client.aclose()


class _Window:
    """Per-second counts over the last ``WINDOW_S`` seconds, in memory."""

    def __init__(self) -> None:
        self._buckets: dict[int, list[int]] = {}

    def add(self, now_s: int, requests: int, retries: int) -> None:
        bucket = self._buckets.setdefault(now_s, [0, 0])
        bucket[0] += requests
        bucket[1] += retries

    def totals(self, now_s: int) -> tuple[int, int]:
        for second in [s for s in self._buckets if s <= now_s - WINDOW_S]:
            del self._buckets[second]
        return (
            sum(b[0] for b in self._buckets.values()),
            sum(b[1] for b in self._buckets.values()),
        )


class RetryBudget:
    def __init__(
        self,
        backend: BudgetBackend | None,
        *,
        clock: Clock | None = None,
        sink: TelemetrySink | None = None,
        flush_interval_s: float = 1.0,
        command_timeout_s: float = 0.25,
        event_interval_s: float = 1.0,
        health: RedisHealth | None = None,
    ) -> None:
        self._backend = backend
        self._clock = SystemClock() if clock is None else clock
        self._sink = sink
        self._flush_interval_s = flush_interval_s
        self._command_timeout_s = command_timeout_s
        self._event_interval_s = event_interval_s
        self._health = health
        self._local: defaultdict[str, _Window] = defaultdict(_Window)
        self._pending: defaultdict[str, int] = defaultdict(int)
        self._last_event: dict[str, float] = {}
        self._last_warned: float | None = None
        self._flusher: asyncio.Task[None] | None = None
        self._closed = False

    def record_request(self, api_id: str) -> None:
        """Count one request. O(1), no await, no I/O."""
        self._local[api_id].add(int(self._clock.now()), 1, 0)
        if self._backend is not None:
            self._pending[api_id] += 1
            if self._flusher is None and not self._closed:
                self._flusher = asyncio.get_running_loop().create_task(self._flush_loop())

    async def try_acquire(
        self, api_id: str, ratio: float, *, project_id: str, request_id: str
    ) -> bool:
        """Take one retry from the budget; False (and one throttled event) when it is spent."""
        now_s = int(self._clock.now())
        allowed: bool | None = None
        if self._backend is not None:
            pending = self._pending.pop(api_id, 0)
            backend = self._backend

            async def acquire() -> bool:
                async with asyncio.timeout(self._command_timeout_s):
                    return await backend.acquire(api_id, now_s, pending, ratio)

            try:
                allowed = await (acquire() if self._health is None else self._health.call(acquire))
            except RedisUnavailable:
                pass  # known down and logged: this instance's counts decide
            except Exception as exc:  # Redis down or slow: fall back to this instance's counts
                self._pending[api_id] += pending
                self._warn(exc)
        if allowed is None:
            requests, retries = self._local[api_id].totals(now_s)
            allowed = retries + 1 <= ratio * requests + _EPSILON
        if allowed:
            self._local[api_id].add(now_s, 0, 1)
        else:
            self._emit_exhausted(api_id, ratio, project_id, request_id)
        return allowed

    async def aclose(self) -> None:
        self._closed = True
        task, self._flusher = self._flusher, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._backend is not None:
            try:
                async with asyncio.timeout(self._command_timeout_s * 2):
                    await self._flush()
            except Exception as exc:
                self._warn(exc)
            await self._backend.aclose()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval_s)
            try:
                async with asyncio.timeout(self._command_timeout_s * 4):
                    await self._flush()
            except Exception as exc:  # cancellation is a BaseException and passes through
                self._warn(exc)

    async def _flush(self) -> None:
        assert self._backend is not None
        if self._health is not None and not self._health.available:
            self._pending.clear()  # the window is 60 s: counts from an outage are not worth keeping
            return
        backend = self._backend
        now_s = int(self._clock.now())
        for api_id in [a for a, n in self._pending.items() if n > 0]:
            count = self._pending.pop(api_id, 0)

            async def add(api: str = api_id, n: int = count) -> None:
                await backend.add_requests(api, now_s, n)

            try:
                await (add() if self._health is None else self._health.call(add))
            except BaseException:
                self._pending[api_id] += count
                raise

    def _warn(self, exc: Exception) -> None:
        now = self._clock.monotonic()
        if self._last_warned is None or now - self._last_warned >= 30.0:
            self._last_warned = now
            log.warning(
                "retry budget: Redis unavailable (%s); using this instance's counts",
                type(exc).__name__,
            )

    def _emit_exhausted(self, api_id: str, ratio: float, project_id: str, request_id: str) -> None:
        if self._sink is None:
            return
        now = self._clock.monotonic()
        last = self._last_event.get(api_id)
        if last is not None and now - last < self._event_interval_s:
            return  # a storm must not flood the stream: at most one event per API per interval
        self._last_event[api_id] = now
        self._sink.emit(
            {
                "v": SCHEMA_VERSION,
                "kind": "event",
                "type": "retry_budget_exhausted",
                "ts": format_ts(self._clock.now()),
                "request_id": request_id,
                "project_id": project_id,
                "api_id": api_id,
                "budget_ratio": ratio,
            }
        )
