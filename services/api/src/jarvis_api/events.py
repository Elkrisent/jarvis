"""Change notifications published to Redis so the gateway reloads its configuration."""

from __future__ import annotations

import json
import logging
from typing import Literal, Protocol

import redis
from redis import asyncio as redis_asyncio

CONFIG_CHANGED_CHANNEL = "config:changed"
CONFIG_INVALIDATED_CHANNEL = "config:invalidated"
# Redis is optional and best-effort: never let an unreachable host stall an API save.
SOCKET_TIMEOUT_SECONDS = 2.0

logger = logging.getLogger(__name__)

InvalidationKind = Literal["api", "key", "project"]


class ConfigPublisher(Protocol):
    async def config_changed(self, api_id: str, version: int) -> None: ...

    async def invalidated(self, kind: InvalidationKind, id: str) -> None: ...


class RedisConfigPublisher:
    """Publishes ``config:changed`` and ``config:invalidated``; never raises."""

    def __init__(self, redis_url: str) -> None:
        self._client = redis_asyncio.from_url(
            redis_url,
            socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
            socket_timeout=SOCKET_TIMEOUT_SECONDS,
        )

    async def config_changed(self, api_id: str, version: int) -> None:
        payload = json.dumps({"api_id": api_id, "version": version}, separators=(",", ":"))
        await self._publish(CONFIG_CHANGED_CHANNEL, payload)

    async def invalidated(self, kind: InvalidationKind, id: str) -> None:
        payload = json.dumps({"kind": kind, "id": id}, separators=(",", ":"))
        await self._publish(CONFIG_INVALIDATED_CHANNEL, payload)

    async def _publish(self, channel: str, payload: str) -> None:
        try:
            await self._client.publish(channel, payload)
        except (redis.RedisError, OSError) as exc:
            logger.warning("could not publish %s (%s)", channel, type(exc).__name__)

    async def aclose(self) -> None:
        await self._client.aclose()
