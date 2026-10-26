"""M2.4: the circuit breaker on real sockets (SPEC §4.8)."""

from __future__ import annotations

import asyncio

import pytest
from jarvis_core.clock import SystemClock
from jarvis_core.memory import InMemoryCircuitStore, InMemoryTelemetrySink
from jarvis_gateway.circuit import CircuitBreaker
from rig import GatewayFactory
from test_retries import OK, _resp, scripted

pytestmark = pytest.mark.functional

POLICY = {"min_calls": 5, "open_duration_s": 1, "max_open_duration_s": 4, "window_s": 30}
NO_RETRY = {"max_attempts": 1}


def _breaker(sink: InMemoryTelemetrySink | None = None) -> CircuitBreaker:
    clock = SystemClock()
    return CircuitBreaker(InMemoryCircuitStore(clock), clock=clock, sink=sink)


async def _trip(gateway, n: int = 5) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        assert (await gateway.call("GET", "/gw/none/x")).status_code == 503
    await gateway.forwarder._circuit.drain()


@pytest.mark.req("REQ-4.8-10", "REQ-4.4-3")
async def test_an_open_circuit_fails_fast_and_the_upstream_gets_no_calls(
    make_gateway: GatewayFactory,
) -> None:
    async with scripted(_resp(503)) as upstream:
        gateway = make_gateway(
            port=upstream.port, circuit_breaker=_breaker(), circuit=POLICY, retry=NO_RETRY
        )
        await _trip(gateway)
        assert len(upstream.heads) == 5
        for _ in range(10):
            response = await gateway.call("GET", "/gw/none/x")
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "JV_CIRCUIT_OPEN"
            assert response.headers["X-Jarvis-Circuit"] == "OPEN"
            assert response.headers["X-Jarvis-Source"] == "jarvis"
        assert len(upstream.heads) == 5  # none of the ten reached it


@pytest.mark.req("REQ-4.8-3")
async def test_4xx_responses_never_open_the_circuit(make_gateway: GatewayFactory) -> None:
    async with scripted(_resp(429), _resp(404)) as upstream:
        gateway = make_gateway(
            port=upstream.port, circuit_breaker=_breaker(), circuit=POLICY, retry=NO_RETRY
        )
        for _ in range(12):
            assert (await gateway.call("GET", "/gw/none/x")).status_code in (404, 429)
        await gateway.forwarder._circuit.drain()
        response = await gateway.call("GET", "/gw/none/x")
        assert response.status_code != 503
        assert response.headers["X-Jarvis-Circuit"] == "CLOSED"


@pytest.mark.req("REQ-4.8-5", "REQ-4.8-7", "REQ-4.8-9")
async def test_after_the_open_duration_one_probe_closes_the_circuit_again(
    make_gateway: GatewayFactory,
) -> None:
    sink = InMemoryTelemetrySink()
    async with scripted(_resp(503), _resp(503), _resp(503), _resp(503), _resp(503), OK) as up:
        gateway = make_gateway(
            port=up.port, circuit_breaker=_breaker(sink), circuit=POLICY, retry=NO_RETRY
        )
        await _trip(gateway)
        assert (await gateway.call("GET", "/gw/none/x")).status_code == 503
        assert len(up.heads) == 5
        await asyncio.sleep(1.1)
        probe = await gateway.call("GET", "/gw/none/x")  # the sixth upstream answer is a 200
        assert probe.status_code == 200
        assert len(up.heads) == 6
        assert probe.headers["X-Jarvis-Circuit"] == "CLOSED"
        assert (await gateway.call("GET", "/gw/none/x")).status_code == 200
        assert len(up.heads) == 7
    moves = [(e["from"], e["to"]) for e in sink.events if isinstance(e, dict)]
    assert moves == [("CLOSED", "OPEN"), ("OPEN", "HALF_OPEN"), ("HALF_OPEN", "CLOSED")]


@pytest.mark.req("REQ-4.8-6", "REQ-4.8-8")
async def test_a_failed_probe_reopens_the_circuit_for_longer(
    make_gateway: GatewayFactory,
) -> None:
    async with scripted(_resp(503)) as upstream:
        gateway = make_gateway(
            port=upstream.port, circuit_breaker=_breaker(), circuit=POLICY, retry=NO_RETRY
        )
        await _trip(gateway)
        await asyncio.sleep(1.1)
        assert (await gateway.call("GET", "/gw/none/x")).status_code == 503  # the probe, failed
        assert len(upstream.heads) == 6
        await asyncio.sleep(1.1)  # the open duration doubled to 2 s: still open
        again = await gateway.call("GET", "/gw/none/x")
        assert again.json()["error"]["code"] == "JV_CIRCUIT_OPEN"
        assert len(upstream.heads) == 6


@pytest.mark.req("REQ-4.8-6")
async def test_only_one_probe_is_sent_for_fifty_concurrent_requests(
    make_gateway: GatewayFactory,
) -> None:
    async def slow_ok(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        calls.append(1)
        await asyncio.sleep(0.7 if len(calls) > 5 else 0)
        writer.write(_resp(503) if len(calls) <= 5 else OK)
        await writer.drain()
        writer.close()

    calls: list[int] = []
    listener = await asyncio.start_server(slow_ok, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        gateway = make_gateway(
            port=port, circuit_breaker=_breaker(), circuit=POLICY, retry=NO_RETRY
        )
        await _trip(gateway)
        await asyncio.sleep(1.1)
        responses = await asyncio.gather(*(gateway.call("GET", "/gw/none/x") for _ in range(50)))
    finally:
        listener.close()
    codes = [
        r.json().get("error", {}).get("code") if r.status_code == 503 else "ok" for r in responses
    ]
    assert codes.count("ok") == 1
    assert codes.count("JV_CIRCUIT_OPEN") == 49
    assert len(calls) == 6  # five that tripped it, one probe


async def test_a_retry_is_not_sent_once_the_circuit_has_opened(
    make_gateway: GatewayFactory,
) -> None:
    async with scripted(_resp(503)) as upstream:
        gateway = make_gateway(
            port=upstream.port,
            circuit_breaker=_breaker(),
            circuit={**POLICY, "min_calls": 1},
            retry={"base_ms": 1, "cap_ms": 2, "max_attempts": 5},
        )
        first = await gateway.call("GET", "/gw/none/x")
        assert first.status_code in (503,)
        await gateway.forwarder._circuit.drain()
        assert len(upstream.heads) < 5  # it stopped retrying when the circuit opened
