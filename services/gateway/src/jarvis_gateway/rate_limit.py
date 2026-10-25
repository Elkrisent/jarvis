"""Token-bucket rate limiters (SPEC §4.11): Redis Lua for the shared bucket, memory as fallback.

The Redis bucket refills and takes in one script, so concurrent callers on any number of gateway
instances can never admit more than the bucket holds. Times come from the injected clock (epoch
seconds), never from the Redis server; a clock that runs behind another instance's only delays a
refill, because the elapsed time is clamped at zero.
"""

from __future__ import annotations

import logging
from typing import Any

from jarvis_core.clock import Clock, SystemClock
from jarvis_core.interfaces import RateDecision, RateLimiter
from redis import asyncio as redis_asyncio

log = logging.getLogger(__name__)

PREFIX = "jarvis:rl:"

_TAKE = """
local cap, rate, cost = tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4])
local now = tonumber(ARGV[1])
local v = redis.call('HMGET', KEYS[1], 't', 'ts')
local tokens, last = tonumber(v[1]), tonumber(v[2])
if tokens == nil then tokens = cap; last = now end
tokens = math.min(cap, tokens + math.max(0, now - last) * rate)
local allowed, retry = 0, -1
if cost > cap then
  retry = -1
elseif tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry = (cost - tokens) / rate
end
redis.call('HSET', KEYS[1], 't', string.format('%.17g', tokens), 'ts', string.format('%.17g', now))
redis.call('EXPIRE', KEYS[1], math.ceil(cap / rate) + 1)
return {allowed, string.format('%.17g', tokens), string.format('%.17g', retry)}
"""


class RedisRateLimiter:
    def __init__(self, url: str, *, clock: Clock | None = None, timeout_s: float = 1.0) -> None:
        self._clock = SystemClock() if clock is None else clock
        # A blocking pool: when more calls are in flight than connections, the rest wait for one
        # (up to ``timeout_s``) instead of failing, which would silently drop to the looser
        # fallback bucket under load.
        pool = redis_asyncio.BlockingConnectionPool.from_url(
            url,
            max_connections=100,
            timeout=timeout_s,
            socket_connect_timeout=timeout_s,
            socket_timeout=timeout_s,
        )
        self._client = redis_asyncio.Redis(connection_pool=pool)
        self._script = self._client.register_script(_TAKE)

    def __repr__(self) -> str:
        return "RedisRateLimiter()"

    async def take(
        self, key: str, *, capacity: int, refill_per_s: float, cost: int = 1
    ) -> RateDecision:
        result: Any = await self._script(
            keys=[f"{PREFIX}{key}"],
            args=[repr(self._clock.now()), capacity, repr(refill_per_s), cost],
        )
        remaining, retry = float(result[1]), float(result[2])
        return RateDecision(
            allowed=bool(result[0]),
            remaining=remaining,
            retry_after_s=retry if retry >= 0 else None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class FallbackRateLimiter:
    """Redis first. If it fails, this instance's own bucket takes over, sized to its share of the
    limit (``limit ÷ expected_instances``); requests keep flowing (SPEC D3)."""

    def __init__(
        self,
        primary: RateLimiter,
        local: RateLimiter,
        *,
        expected_instances: int = 1,
        clock: Clock | None = None,
    ) -> None:
        if expected_instances < 1:
            raise ValueError("expected_instances must be at least 1")
        self._primary = primary
        self._local = local
        self._instances = expected_instances
        self._clock = SystemClock() if clock is None else clock
        self._last_warned: float | None = None

    async def take(
        self, key: str, *, capacity: int, refill_per_s: float, cost: int = 1
    ) -> RateDecision:
        try:
            return await self._primary.take(
                key, capacity=capacity, refill_per_s=refill_per_s, cost=cost
            )
        except Exception as exc:
            self._warn(exc)
        share = max(1, capacity // self._instances)
        return await self._local.take(
            f"local:{key}",
            capacity=share,
            refill_per_s=refill_per_s / self._instances,
            cost=cost,
        )

    async def aclose(self) -> None:
        closer = getattr(self._primary, "aclose", None)
        if closer is not None:
            await closer()

    def _warn(self, exc: Exception) -> None:
        now = self._clock.monotonic()
        if self._last_warned is None or now - self._last_warned >= 30.0:
            self._last_warned = now
            log.warning(
                "rate limiter: Redis unavailable (%s); using this instance's share of the limit",
                type(exc).__name__,
            )
