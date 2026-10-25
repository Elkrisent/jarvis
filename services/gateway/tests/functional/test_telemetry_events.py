"""One telemetry event per request, whatever happens, and never a secret in it."""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

import pytest
from jarvis_core.memory import InMemoryTelemetrySink
from rig import KEY, SECRET, GatewayFactory, Upstream

pytestmark = pytest.mark.functional

UUID = "550e8400-e29b-41d4-a716-446655440000"


def only(sink: InMemoryTelemetrySink) -> dict[str, Any]:
    assert len(sink.events) == 1, sink.events
    return sink.events[0]


@pytest.mark.req("REQ-6.1-2")
async def test_a_successful_call_is_one_complete_event(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    sink = InMemoryTelemetrySink()
    gateway = make_gateway(sink=sink)
    await upstream.rules({"path_prefix": "/", "latency_ms": 20, "body_bytes": 5000})
    response = await gateway.call("POST", "/gw/bearer/v1/items/42?q=1", content=b"x" * 11)
    assert response.status_code == 200
    event = only(sink)
    assert event["kind"] == "request"
    assert event["v"] == 1
    assert event["request_id"] == response.headers["X-Jarvis-Request-Id"]
    assert (event["project_id"], event["key_id"], event["api_id"]) == ("p1", "k1", "bearer-api")
    assert event["method"] == "POST"
    assert event["route"] == "/v1/items/{id}"
    assert event["status"] == 200
    assert event["source"] == "upstream"
    assert event["attempts"] == 1
    assert event["req_bytes"] == 11
    assert event["resp_bytes"] == len(response.content) == 5000
    assert event["upstream_latency_ms"] >= 15
    assert event["latency_ms"] >= event["upstream_latency_ms"]
    assert event["error_code"] is None
    assert event["ts"].endswith("Z")
    assert event["cache"] is None
    assert event["circuit"] is None
    assert event["dedup_role"] is None


@pytest.mark.req("REQ-6.1-2")
@pytest.mark.parametrize("status", [404, 429, 500, 503])
async def test_an_upstream_error_passes_through_without_a_jarvis_error_code(
    make_gateway: GatewayFactory, upstream: Upstream, status: int
) -> None:
    sink = InMemoryTelemetrySink()
    gateway = make_gateway(sink=sink)
    await upstream.rules({"path_prefix": "/", "status": status})
    assert (await gateway.call("GET", "/gw/none/x")).status_code == status
    event = only(sink)
    assert event["status"] == status
    assert event["source"] == "upstream"
    assert event["error_code"] is None


@pytest.mark.req("REQ-6.1-2")
@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
async def test_a_response_without_a_body_counts_zero_bytes(
    make_gateway: GatewayFactory, upstream: Upstream, method: str
) -> None:
    sink = InMemoryTelemetrySink()
    gateway = make_gateway(sink=sink)
    await upstream.rules({"path_prefix": "/", "status": 204})
    assert (await gateway.call(method, "/gw/none/x")).status_code == 204
    event = only(sink)
    assert event["resp_bytes"] == 0
    assert event["status"] == 204


@pytest.mark.req("REQ-6.1-2")
async def test_a_large_streamed_response_is_finished_after_the_last_chunk(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    sink = InMemoryTelemetrySink()
    gateway = make_gateway(sink=sink)
    size = 5 * 1024 * 1024
    await upstream.rules({"path_prefix": "/", "body_bytes": size})
    response = await gateway.call("GET", "/gw/none/big")
    assert len(response.content) == size
    assert only(sink)["resp_bytes"] == size


@pytest.mark.req("REQ-6.1-2")
async def test_an_upstream_timeout_is_a_jarvis_error_event(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    sink = InMemoryTelemetrySink()
    gateway = make_gateway(sink=sink, deadline_ms=300, attempt_timeout_ms=200)
    await upstream.rules({"path_prefix": "/", "hang": True})
    assert (await gateway.call("GET", "/gw/none/slow")).status_code == 504
    event = only(sink)
    assert (event["status"], event["source"], event["error_code"]) == (
        504,
        "jarvis",
        "JV_UPSTREAM_TIMEOUT",
    )
    assert event["attempts"] == 1
    assert event["upstream_latency_ms"] is None


@pytest.mark.req("REQ-6.1-2")
async def test_an_unreachable_upstream_is_a_jarvis_error_event(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    from rig import api_entry, free_port

    sink = InMemoryTelemetrySink()
    gateway = make_gateway(
        sink=sink, apis=[api_entry("dead-api", "dead", {"mode": "none"}, free_port())]
    )
    assert (await gateway.call("GET", "/gw/dead/x")).status_code == 502
    event = only(sink)
    assert event["error_code"] == "JV_UPSTREAM_UNREACHABLE"
    assert event["attempts"] == 3  # a GET is retried on a connection error (default max_attempts)


@pytest.mark.req("REQ-6.1-2")
@pytest.mark.parametrize(
    ("kwargs", "status", "code"),
    [
        ({"key": None}, 401, "JV_AUTH_INVALID"),
        ({"key": "jv_live_wrongwrongwrong"}, 401, "JV_AUTH_INVALID"),
        ({"headers": {"Accept": "text/event-stream"}}, 400, "JV_STREAMING_UNSUPPORTED"),
        ({"content": b"x" * (64 * 1024 + 1), "method": "POST"}, 413, "JV_BODY_TOO_LARGE"),
    ],
)
async def test_requests_refused_before_forwarding_still_get_an_event(
    make_gateway: GatewayFactory,
    upstream: Upstream,
    kwargs: dict[str, Any],
    status: int,
    code: str,
) -> None:
    sink = InMemoryTelemetrySink()
    gateway = make_gateway(sink=sink)
    method = kwargs.pop("method", "GET")
    response = await gateway.call(method, "/gw/none/x", **kwargs)
    assert response.status_code == status
    event = only(sink)
    assert (event["status"], event["source"], event["error_code"]) == (status, "jarvis", code)
    assert event["attempts"] == 0
    assert event["request_id"] == response.headers["X-Jarvis-Request-Id"]
    assert await upstream.calls() == []


@pytest.mark.req("REQ-6.1-2")
async def test_a_body_too_large_after_authentication_keeps_its_ids(
    make_gateway: GatewayFactory,
) -> None:
    sink = InMemoryTelemetrySink()
    gateway = make_gateway(sink=sink)

    async def chunks() -> Any:
        for _ in range(100):
            yield b"x" * 1024  # no Content-Length: capped while it is read

    response = await gateway.call("POST", "/gw/none/x", content=chunks())
    assert response.status_code == 413
    event = only(sink)
    assert event["error_code"] == "JV_BODY_TOO_LARGE"
    assert event["api_id"] == "none-api"
    assert event["attempts"] == 0


@pytest.mark.req("REQ-6.1-2")
async def test_a_snapshot_that_is_not_loaded_yet_still_gets_an_event(
    make_gateway: GatewayFactory,
) -> None:
    sink = InMemoryTelemetrySink()
    gateway = make_gateway(sink=sink)
    gateway.store._current = None  # type: ignore[attr-defined]  # simulate "before the first load"
    response = await gateway.call("GET", "/gw/none/x")
    assert response.status_code == 503
    assert only(sink)["error_code"] == "JV_NOT_READY"


async def test_the_health_endpoint_emits_nothing(make_gateway: GatewayFactory) -> None:
    sink = InMemoryTelemetrySink()
    gateway = make_gateway(sink=sink)
    assert (await gateway.client.get("/healthz")).status_code == 200
    assert sink.events == []


@pytest.mark.req("REQ-6.1-2")
async def test_a_failing_sink_does_not_change_the_response(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    full = InMemoryTelemetrySink(capacity=0)  # every emit is a drop
    gateway = make_gateway(sink=full)
    response = await gateway.call("GET", "/gw/bearer/x")
    assert response.status_code == 200
    assert full.dropped_total == 1


@pytest.mark.req("REQ-11.2-4")
async def test_no_secret_query_or_raw_id_reaches_an_event_or_a_log(
    make_gateway: GatewayFactory, upstream: Upstream, caplog: pytest.LogCaptureFixture
) -> None:
    sink = InMemoryTelemetrySink()
    gateway = make_gateway(sink=sink)
    caplog.set_level(logging.DEBUG)
    sentinel = "SENTINEL-QUERY-VALUE-77"
    for mode in ("bearer", "header", "query", "none"):
        response = await gateway.call(
            "GET",
            f"/gw/{mode}/v1/users/{UUID}/orders?token={sentinel}&appid=client-supplied",
            headers={"X-Custom": sentinel},
        )
        assert response.status_code == 200
    assert len(sink.events) == 4
    blob = json.dumps(sink.events) + caplog.text
    for forbidden in (SECRET, KEY, sentinel, UUID, "client-supplied"):
        assert forbidden not in blob
    assert {e["route"] for e in sink.events} == {"/v1/users/{id}/orders"}


@pytest.mark.req("REQ-6.1-2")
async def test_two_requests_produce_two_events_with_their_own_ids(
    make_gateway: GatewayFactory,
) -> None:
    sink = InMemoryTelemetrySink()
    gateway = make_gateway(sink=sink)
    first = await gateway.call("GET", "/gw/none/a")
    second = await gateway.call("GET", "/gw/none/b")
    ids = [e["request_id"] for e in sink.events]
    assert ids == [first.headers["X-Jarvis-Request-Id"], second.headers["X-Jarvis-Request-Id"]]
    assert len(set(ids)) == 2


async def _drive_asgi(
    app: Any, path: str, disconnect_after_s: float | None
) -> list[dict[str, Any]]:
    import asyncio

    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"x-jarvis-key", KEY.encode()), (b"host", b"gateway")],
        "client": ("127.0.0.1", 1),
        "server": ("gateway", 80),
    }
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        if disconnect_after_s is None:
            await asyncio.Event().wait()
        await asyncio.sleep(disconnect_after_s or 0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body":
            await asyncio.sleep(0.01)  # a slow reader, so the disconnect lands mid-body

    await app(scope, receive, send)
    return sent


@pytest.mark.req("REQ-6.1-2")
async def test_a_client_that_disconnects_mid_stream_still_gets_one_event(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    sink = InMemoryTelemetrySink()
    gateway = make_gateway(sink=sink)
    size = 20 * 1024 * 1024
    await upstream.rules({"path_prefix": "/", "body_bytes": size})
    sent = await _drive_asgi(gateway.app, "/gw/none/big", disconnect_after_s=0.2)
    assert sent[0]["status"] == 200
    event = only(sink)
    assert event["status"] == 200
    assert event["source"] == "upstream"
    assert 0 < event["resp_bytes"] < size


@pytest.mark.req("REQ-6.1-2")
async def test_an_upstream_cut_mid_body_still_gets_one_event(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    sink = InMemoryTelemetrySink()
    gateway = make_gateway(sink=sink)
    await upstream.rules({"path_prefix": "/", "reset": True})
    with contextlib.suppress(Exception):  # the caller sees the cut; only the event matters here
        await gateway.call("GET", "/gw/none/cut")
    event = only(sink)
    assert event["status"] == 200
    assert event["error_code"] is None
