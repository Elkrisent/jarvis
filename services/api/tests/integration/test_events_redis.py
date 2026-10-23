"""config:changed reaches a real Redis subscriber."""

from __future__ import annotations

import json
import os

import pytest
from jarvis_api.events import CONFIG_CHANGED_CHANNEL, RedisConfigPublisher
from redis import asyncio as redis_asyncio

pytestmark = [pytest.mark.integration, pytest.mark.req("REQ-5.2-3")]


async def test_config_changed_is_published_to_redis() -> None:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.fail("REDIS_URL must point at a Redis server to run integration tests")
    client = redis_asyncio.from_url(url)
    pubsub = client.pubsub()
    publisher = RedisConfigPublisher(url)
    try:
        await pubsub.subscribe(CONFIG_CHANGED_CHANNEL)
        confirmation = await pubsub.get_message(timeout=5)  # subscribe is acknowledged
        assert confirmation is not None
        assert confirmation["type"] == "subscribe"
        await publisher.config_changed("api_1", 3)
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5)
        assert message is not None
        assert json.loads(message["data"]) == {"api_id": "api_1", "version": 3}
    finally:
        await publisher.aclose()
        await pubsub.aclose()
        await client.aclose()
