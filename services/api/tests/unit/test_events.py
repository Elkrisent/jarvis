"""The config:changed publisher never raises and never leaks connection details."""

from __future__ import annotations

import logging

import pytest
import redis
from jarvis_api import events
from jarvis_api.events import RedisConfigPublisher

URL = "redis://:pw@host:6379/0"


class _FailingRedis:
    async def publish(self, channel: str, message: str) -> int:
        raise redis.ConnectionError(URL)

    async def aclose(self) -> None:
        return None


@pytest.mark.unit
@pytest.mark.req("REQ-5.2-3")
async def test_publish_failure_is_logged_without_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(events.redis_asyncio, "from_url", lambda url: _FailingRedis())
    publisher = RedisConfigPublisher(URL)

    with caplog.at_level(logging.WARNING):
        await publisher.config_changed("api_1", 3)

    assert "could not publish config:changed (ConnectionError)" in caplog.text
    assert "pw" not in caplog.text
    assert URL not in caplog.text
