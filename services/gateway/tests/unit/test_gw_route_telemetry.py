"""The /gw route finishes its telemetry record on unexpected failures and cancellation."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from jarvis_core.config import ApiDependency
from jarvis_core.keys import hash_jarvis_key
from jarvis_core.memory import InMemoryTelemetrySink
from jarvis_gateway.app import create_app
from jarvis_gateway.config.manager import ConfigManager
from jarvis_gateway.config.snapshot import ApiEntry, KeyEntry, Snapshot
from jarvis_gateway.config.store import ConfigStore
from jarvis_gateway.forwarder import Forwarder

pytestmark = pytest.mark.unit

KEY = "jv_live_goodkey000"


class StoreOnly:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class ExplodingForwarder:
    max_body_bytes = 1024

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def forward(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error


def build(error: BaseException) -> tuple[TestClient, InMemoryTelemetrySink]:
    store = ConfigStore()
    raw: dict[str, Any] = {"slug": "weather", "name": "w", "base_url": "https://api.example.com"}
    api = ApiEntry("a1", "p1", "weather", 1, ApiDependency.model_validate(raw))
    store.replace(
        Snapshot.empty("postgres", 1.0).with_changes(
            apis={"a1": api}, keys={hash_jarvis_key(KEY): KeyEntry("k1", "p1", frozenset(), False)}
        )
    )
    sink = InMemoryTelemetrySink()
    app = create_app(
        config=cast(ConfigManager, StoreOnly(store)),
        forwarder=cast(Forwarder, ExplodingForwarder(error)),
        sink=sink,
    )
    return TestClient(app, raise_server_exceptions=False), sink


@pytest.mark.req("REQ-6.1-2")
def test_an_unexpected_error_is_a_500_event() -> None:
    client, sink = build(RuntimeError("bug"))
    response = client.get("/gw/weather/x", headers={"X-Jarvis-Key": KEY})
    assert response.status_code == 500
    assert len(sink.events) == 1
    event = sink.events[0]
    assert (event["status"], event["source"], event["error_code"]) == (500, "jarvis", "JV_INTERNAL")
    assert (event["project_id"], event["key_id"], event["api_id"]) == ("p1", "k1", "a1")
    assert event["route"] == "/x"


@pytest.mark.req("REQ-6.1-2")
def test_a_cancelled_request_is_a_499_event() -> None:
    client, sink = build(asyncio.CancelledError())
    with contextlib.suppress(BaseException):  # the test client may or may not re-raise it
        client.get("/gw/weather/x", headers={"X-Jarvis-Key": KEY})
    assert len(sink.events) == 1
    assert sink.events[0]["status"] == 499
    assert sink.events[0]["error_code"] is None
