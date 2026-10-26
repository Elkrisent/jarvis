"""M2.6: fallback to another API of the project (SPEC §4.12 step 2) on real sockets."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from jarvis_core.clock import SystemClock
from jarvis_core.interfaces import CircuitSnapshot, CircuitState
from jarvis_core.memory import InMemoryCircuitStore, InMemoryTelemetrySink
from jarvis_gateway.circuit import CircuitBreaker
from rig import SECRET, GatewayFactory, api_entry, free_port
from test_retries import OK, _resp, scripted

pytestmark = pytest.mark.functional

NO_RETRY = {"max_attempts": 1}


def _apis(primary_port: int, backup_port: int, **primary_cfg: Any) -> list[Any]:
    primary_cfg.setdefault("retry", NO_RETRY)
    return [
        api_entry(
            "main-api",
            "main",
            {"mode": "none"},
            primary_port,
            fallback={"fallback_api": "backup"},
            **primary_cfg,
        ),
        api_entry("backup-api", "backup", {"mode": "bearer"}, backup_port, retry=NO_RETRY),
    ]


@pytest.mark.req("REQ-4.12-2")
@pytest.mark.parametrize("failure", [_resp(500), _resp(502), _resp(503), _resp(504)])
async def test_a_failing_primary_is_served_by_the_fallback_api(
    make_gateway: GatewayFactory, failure: bytes
) -> None:
    async with scripted(failure) as primary, scripted(OK) as backup:
        gateway = make_gateway(apis=_apis(primary.port, backup.port))
        response = await gateway.call("GET", "/gw/main/v1/x?q=1")
        assert response.status_code == 200
        assert response.text == "ok"
        assert response.headers["X-Jarvis-Source"] == "fallback"
        assert response.headers["X-Jarvis-Attempts"] == "2"  # one on each API
        assert len(primary.heads) == 1
        assert len(backup.heads) == 1
        # the same path and query, against the fallback API's own base URL, with its own credential
        assert backup.heads[0].startswith("GET /base/v1/x?q=1 HTTP/1.1")
        assert backup.header(0, "authorization") == f"Bearer {SECRET}"
        assert primary.header(0, "authorization") is None


@pytest.mark.req("REQ-4.12-2")
async def test_an_unreachable_or_hung_primary_falls_back(make_gateway: GatewayFactory) -> None:
    async with scripted(OK) as backup:
        gateway = make_gateway(apis=_apis(free_port(), backup.port))
        assert (await gateway.call("GET", "/gw/main/x")).headers["X-Jarvis-Source"] == "fallback"

    async def hang(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(30)

    listener = await asyncio.start_server(hang, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        async with scripted(OK) as backup:
            gateway = make_gateway(
                apis=_apis(port, backup.port, attempt_timeout_ms=300, deadline_ms=3000)
            )
            response = await gateway.call("GET", "/gw/main/x")
            assert response.status_code == 200
            assert response.headers["X-Jarvis-Source"] == "fallback"
    finally:
        listener.close()


@pytest.mark.req("REQ-4.12-2", "REQ-8-7")
async def test_with_the_primary_circuit_open_the_fallback_serves_and_the_primary_gets_no_call(
    make_gateway: GatewayFactory,
) -> None:
    clock = SystemClock()
    store = InMemoryCircuitStore(clock)
    await store.set("main-api", CircuitSnapshot(CircuitState.OPEN, clock.now(), 3600))
    async with scripted(OK) as primary, scripted(OK) as backup:
        gateway = make_gateway(
            apis=_apis(primary.port, backup.port),
            circuit_breaker=CircuitBreaker(store, clock=clock),
        )
        response = await gateway.call("GET", "/gw/main/x")
        assert response.status_code == 200
        assert response.headers["X-Jarvis-Source"] == "fallback"
        assert len(primary.heads) == 0
        assert len(backup.heads) == 1


@pytest.mark.req("REQ-4.12-4")
async def test_when_both_fail_the_error_is_a_controlled_jarvis_error(
    make_gateway: GatewayFactory,
) -> None:
    clock = SystemClock()
    store = InMemoryCircuitStore(clock)
    await store.set("main-api", CircuitSnapshot(CircuitState.OPEN, clock.now(), 3600))
    async with scripted(_resp(503)) as backup:
        gateway = make_gateway(
            apis=_apis(free_port(), backup.port),
            circuit_breaker=CircuitBreaker(store, clock=clock),
        )
        response = await gateway.call("GET", "/gw/main/x")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "JV_CIRCUIT_OPEN"  # the primary's own reason
        assert response.headers["X-Jarvis-Source"] == "jarvis"
        assert len(backup.heads) == 1


@pytest.mark.req("REQ-4.12-4")
async def test_when_both_fail_and_the_primary_answered_its_own_answer_is_passed_through(
    make_gateway: GatewayFactory,
) -> None:
    async with scripted(_resp(503, body=b"primary says no")) as primary, scripted(_resp(500)) as b:
        gateway = make_gateway(apis=_apis(primary.port, b.port))
        response = await gateway.call("GET", "/gw/main/x")
        assert response.status_code == 503
        assert response.text == "primary says no"
        assert response.headers["X-Jarvis-Source"] == "upstream"
        assert len(b.heads) == 1


@pytest.mark.req("REQ-4.12-2")
async def test_client_errors_and_rate_limits_do_not_trigger_the_fallback(
    make_gateway: GatewayFactory,
) -> None:
    for status in (400, 404, 429):
        async with scripted(_resp(status)) as primary, scripted(OK) as backup:
            gateway = make_gateway(apis=_apis(primary.port, backup.port))
            response = await gateway.call("GET", "/gw/main/x")
            assert response.status_code == status
            assert response.headers["X-Jarvis-Source"] == "upstream"
            assert backup.heads == []


@pytest.mark.req("REQ-4.12-3")
async def test_a_chain_is_never_followed_even_if_the_config_has_one(
    make_gateway: GatewayFactory,
) -> None:
    async with scripted(_resp(503)) as a, scripted(_resp(503)) as b, scripted(OK) as c:
        entries = [
            api_entry(
                "a-api",
                "main",
                {"mode": "none"},
                a.port,
                retry=NO_RETRY,
                fallback={"fallback_api": "mid"},
            ),
            api_entry(
                "b-api",
                "mid",
                {"mode": "none"},
                b.port,
                retry=NO_RETRY,
                fallback={"fallback_api": "last"},
            ),  # a chain the API should have refused
            api_entry("c-api", "last", {"mode": "none"}, c.port, retry=NO_RETRY),
        ]
        gateway = make_gateway(apis=entries)
        response = await gateway.call("GET", "/gw/main/x")
        assert response.status_code == 503  # a failed, mid failed, last was never asked
        assert len(c.heads) == 0


async def test_a_missing_fallback_api_is_ignored_and_the_primary_answer_stands(
    make_gateway: GatewayFactory,
) -> None:
    async with scripted(_resp(503)) as primary:
        entry = api_entry(
            "main-api",
            "main",
            {"mode": "none"},
            primary.port,
            retry=NO_RETRY,
            fallback={"fallback_api": "gone"},
        )
        gateway = make_gateway(apis=[entry])
        response = await gateway.call("GET", "/gw/main/x")
        assert response.status_code == 503
        assert response.headers["X-Jarvis-Source"] == "upstream"


async def test_the_fallback_shares_the_request_deadline(make_gateway: GatewayFactory) -> None:
    async def slow_503(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(0.5)
        writer.write(_resp(503))
        await writer.drain()
        writer.close()

    listener = await asyncio.start_server(slow_503, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        async with scripted(OK) as backup:
            # 600 ms deadline: the primary takes 500 ms, leaving 100 ms, under min_attempt_ms
            gateway = make_gateway(
                apis=_apis(
                    port, backup.port, deadline_ms=600, attempt_timeout_ms=600, min_attempt_ms=300
                )
            )
            response = await gateway.call("GET", "/gw/main/x")
            assert response.status_code == 503  # the primary's answer; no time for a fallback
            assert backup.heads == []
    finally:
        listener.close()


@pytest.mark.req("REQ-4.12-2", "REQ-6.1-3")
async def test_a_fallback_emits_an_event_and_the_request_event_says_fallback(
    make_gateway: GatewayFactory,
) -> None:
    sink = InMemoryTelemetrySink()
    async with scripted(_resp(503)) as primary, scripted(OK) as backup:
        gateway = make_gateway(apis=_apis(primary.port, backup.port), sink=sink)
        gateway.forwarder._events = sink
        await gateway.call("GET", "/gw/main/x")
        await asyncio.sleep(0.05)
    events = [e for e in sink.events if isinstance(e, dict)]
    [fallback] = [e for e in events if e.get("type") == "fallback"]
    assert fallback["kind"] == "event"
    assert (fallback["api_id"], fallback["fallback_api_id"]) == ("main-api", "backup-api")
    assert fallback["reason"] == "upstream_5xx"
    assert fallback["served"] is True
    requests = [e for e in events if e.get("kind") == "request"]
    assert [(e["source"], e["status"], e["attempts"]) for e in requests] == [("fallback", 200, 2)]
