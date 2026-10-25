"""``CircuitStore`` on Redis (SPEC §4.8): shared state, windowed call counts, one probe lock.

All times come from the injected clock (passed to the scripts), never from the Redis server, so a
fake clock drives the same behaviour in tests. Keys also carry a real TTL so abandoned APIs vanish.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from jarvis_core.clock import Clock, SystemClock
from jarvis_core.interfaces import CallStats, CircuitSnapshot, CircuitState, CircuitStore
from redis import asyncio as redis_asyncio

log = logging.getLogger(__name__)

PREFIX = "jarvis:cb:"
CHANNEL = "circuit:changed"
_STATE_TTL_S = 7 * 24 * 3600

# Per second: a hash {t: calls, f: failures}. Count the last ``window`` seconds.
_RECORD = """
local prefix, now, failed, window = ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4])
local bucket = prefix .. now
redis.call('HINCRBY', bucket, 't', 1)
if failed == 1 then redis.call('HINCRBY', bucket, 'f', 1) end
redis.call('EXPIRE', bucket, window + 1)
local total, failures = 0, 0
for s = now - window + 1, now do
  local v = redis.call('HMGET', prefix .. s, 't', 'f')
  total = total + (tonumber(v[1]) or 0)
  failures = failures + (tonumber(v[2]) or 0)
end
return {total, failures}
"""

# The probe slot holds its expiry time; a stale holder (crashed, or past its TTL) can be replaced.
_PROBE = """
local held = redis.call('GET', KEYS[1])
if held and tonumber(held) > tonumber(ARGV[1]) then return 0 end
redis.call('SET', KEYS[1], ARGV[1] + ARGV[2], 'EX', ARGV[2] + 60)
return 1
"""


class RedisCircuitStore:
    def __init__(self, url: str, *, clock: Clock | None = None, timeout_s: float = 1.0) -> None:
        self._clock = SystemClock() if clock is None else clock
        self._client = redis_asyncio.from_url(
            url, socket_connect_timeout=timeout_s, socket_timeout=timeout_s
        )
        self._record = self._client.register_script(_RECORD)
        self._probe = self._client.register_script(_PROBE)
        self._url = url

    def __repr__(self) -> str:
        return "RedisCircuitStore()"

    async def get(self, api_id: str) -> CircuitSnapshot:
        raw: Any = await self._client.hgetall(f"{PREFIX}{api_id}:state")
        if not raw:
            return CircuitSnapshot()
        fields = {k.decode(): v.decode() for k, v in raw.items()}
        return CircuitSnapshot(
            CircuitState(fields["state"]),
            _opt_float(fields.get("opened_at")),
            int(fields.get("open_duration_s", "0")),
            _opt_float(fields.get("closed_at")),
        )

    async def set(self, api_id: str, snapshot: CircuitSnapshot) -> None:
        key = f"{PREFIX}{api_id}:state"
        mapping: Any = {
            "state": snapshot.state.value,
            "open_duration_s": str(snapshot.open_duration_s),
        }
        if snapshot.opened_at is not None:
            mapping["opened_at"] = repr(snapshot.opened_at)
        if snapshot.closed_at is not None:
            mapping["closed_at"] = repr(snapshot.closed_at)
        pipe = self._client.pipeline(transaction=True)
        pipe.delete(key)
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, _STATE_TTL_S)
        await pipe.execute()

    async def record_call(self, api_id: str, *, success: bool, window_s: int) -> CallStats:
        result: Any = await self._record(
            args=[f"{PREFIX}{api_id}:calls:", int(self._clock.now()), 0 if success else 1, window_s]
        )
        return CallStats(total=int(result[0]), failures=int(result[1]))

    async def try_acquire_probe(self, api_id: str, *, ttl_s: int) -> bool:
        result: Any = await self._probe(
            keys=[f"{PREFIX}{api_id}:probe"], args=[self._clock.now(), ttl_s]
        )
        return bool(result)

    async def release_probe(self, api_id: str) -> None:
        await self._client.delete(f"{PREFIX}{api_id}:probe")

    async def publish_change(self, api_id: str) -> None:
        await self._client.publish(CHANNEL, api_id)

    async def listen(
        self, on_change: Callable[[str], Awaitable[None]], stop: asyncio.Event
    ) -> None:
        """Call ``on_change(api_id)`` for every published state change until ``stop`` is set,
        reconnecting after any failure. Never raises (cancellation excepted)."""
        backoff = 0.5
        while not stop.is_set():
            client = redis_asyncio.from_url(
                self._url, health_check_interval=5, socket_connect_timeout=5, socket_keepalive=True
            )
            pubsub = client.pubsub()
            try:
                await pubsub.subscribe(CHANNEL)
                backoff = 0.5
                while not stop.is_set():
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message is not None and message.get("type") == "message":
                        data = message["data"]
                        text = data.decode() if isinstance(data, bytes) else str(data)
                        if text:
                            await on_change(text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("circuit subscriber lost Redis (%s)", type(exc).__name__)
            finally:
                await _quiet_close(pubsub, client)
            if not stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    async def aclose(self) -> None:
        await self._client.aclose()


async def _quiet_close(pubsub: Any, client: Any) -> None:
    try:
        await pubsub.aclose()
    finally:
        await client.aclose()


def _opt_float(value: str | None) -> float | None:
    return None if value is None else float(value)


class FallbackCircuitStore:
    """Redis first; any failure uses the in-memory store instead (SPEC D3: local circuit state)."""

    def __init__(
        self, primary: CircuitStore, fallback: CircuitStore, *, clock: Clock | None = None
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._clock = SystemClock() if clock is None else clock
        self._last_warned: float | None = None

    async def _run[T](self, name: str, *args: Any, **kwargs: Any) -> T:
        try:
            async with asyncio.timeout(0.5):
                result: T = await getattr(self._primary, name)(*args, **kwargs)
                return result
        except Exception as exc:
            now = self._clock.monotonic()
            if self._last_warned is None or now - self._last_warned >= 30.0:
                self._last_warned = now
                log.warning(
                    "circuit store: Redis unavailable (%s); using local state", type(exc).__name__
                )
            fallback: T = await getattr(self._fallback, name)(*args, **kwargs)
            return fallback

    async def get(self, api_id: str) -> CircuitSnapshot:
        return await self._run("get", api_id)

    async def set(self, api_id: str, snapshot: CircuitSnapshot) -> None:
        await self._run("set", api_id, snapshot)

    async def record_call(self, api_id: str, *, success: bool, window_s: int) -> CallStats:
        return await self._run("record_call", api_id, success=success, window_s=window_s)

    async def try_acquire_probe(self, api_id: str, *, ttl_s: int) -> bool:
        return await self._run("try_acquire_probe", api_id, ttl_s=ttl_s)

    async def release_probe(self, api_id: str) -> None:
        await self._run("release_probe", api_id)
