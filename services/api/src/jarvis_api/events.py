"""Change notifications published to Redis so the gateway reloads its configuration."""

from __future__ import annotations

import json
import logging
from typing import Protocol

import redis
from redis import asyncio as redis_asyncio

CONFIG_CHANGED_CHANNEL = "config:changed"
# Redis is optional and best-effort: never let an unreachable host stall an API save.
SOCKET_TIMEOUT_SECONDS = 2.0

logger = logging.getLogger(__name__)


class ConfigPublisher(Protocol):
    async def config_changed(self, api_id: str, version: int) -> None: ...


class RedisConfigPublisher:
    """Publishes ``config:changed`` events; a failure is logged and never raised."""

    def __init__(self, redis_url: str) -> None:
        self._client = redis_asyncio.from_url(
            redis_url,
            socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
            socket_timeout=SOCKET_TIMEOUT_SECONDS,
        )

    async def config_changed(self, api_id: str, version: int) -> None:
        payload = json.dumps({"api_id": api_id, "version": version}, separators=(",", ":"))
        try:
            await self._client.publish(CONFIG_CHANGED_CHANNEL, payload)
        except (redis.RedisError, OSError) as exc:
            logger.warning("could not publish config:changed (%s)", type(exc).__name__)

    async def aclose(self) -> None:
        await self._client.aclose()
