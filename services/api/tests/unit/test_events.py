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
    monkeypatch.setattr(events.redis_asyncio, "from_url", lambda url, **kwargs: _FailingRedis())
    publisher = RedisConfigPublisher(URL)

    with caplog.at_level(logging.WARNING):
        await publisher.config_changed("api_1", 3)

    assert "could not publish config:changed (ConnectionError)" in caplog.text
    assert "pw" not in caplog.text
    assert URL not in caplog.text


class _RaisingRedis:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def publish(self, channel: str, message: str) -> int:
        raise self._exc


@pytest.mark.unit
@pytest.mark.req("REQ-5.2-3")
def test_client_has_bounded_connect_and_socket_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_from_url(url: str, **kwargs: object) -> _FailingRedis:
        seen.update(kwargs)
        return _FailingRedis()

    monkeypatch.setattr(events.redis_asyncio, "from_url", fake_from_url)
    RedisConfigPublisher(URL)

    for name in ("socket_connect_timeout", "socket_timeout"):
        value = seen.get(name)
        assert isinstance(value, float)
        assert 0 < value <= 2.0


@pytest.mark.unit
@pytest.mark.req("REQ-5.2-3")
@pytest.mark.parametrize(
    ("exc", "name"),
    [(redis.TimeoutError("slow"), "TimeoutError"), (OSError("down"), "OSError")],
)
async def test_timeout_and_os_errors_are_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, exc: Exception, name: str
) -> None:
    monkeypatch.setattr(events.redis_asyncio, "from_url", lambda url, **kw: _RaisingRedis(exc))
    publisher = RedisConfigPublisher(URL)

    with caplog.at_level(logging.WARNING):
        await publisher.config_changed("api_1", 3)

    assert f"could not publish config:changed ({name})" in caplog.text


class _RecordingRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1


@pytest.mark.unit
@pytest.mark.req("REQ-11.1-6")
async def test_invalidated_publishes_compact_json_on_its_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RecordingRedis()
    monkeypatch.setattr(events.redis_asyncio, "from_url", lambda url, **kw: client)

    await RedisConfigPublisher(URL).invalidated("key", "key_1")

    assert client.published == [("config:invalidated", '{"kind":"key","id":"key_1"}')]
    assert events.CONFIG_INVALIDATED_CHANNEL == "config:invalidated"


@pytest.mark.unit
@pytest.mark.req("REQ-11.1-6")
async def test_invalidated_failure_is_logged_without_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(events.redis_asyncio, "from_url", lambda url, **kw: _FailingRedis())
    publisher = RedisConfigPublisher(URL)

    with caplog.at_level(logging.WARNING):
        await publisher.invalidated("project", "prj_1")

    assert "could not publish config:invalidated (ConnectionError)" in caplog.text
    assert "pw" not in caplog.text
    assert URL not in caplog.text
    assert "prj_1" not in caplog.text
