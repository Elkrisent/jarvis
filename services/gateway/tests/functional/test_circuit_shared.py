"""M2.4: two gateway instances share one circuit through Redis. Needs REDIS_URL."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from jarvis_core.clock import SystemClock
from jarvis_core.memory import InMemoryTelemetrySink
from jarvis_gateway.circuit import CircuitBreaker
from jarvis_gateway.circuit_store import PREFIX, RedisCircuitStore
from redis import asyncio as redis_asyncio
from rig import GatewayFactory, api_entry
from test_retries import OK, _resp

pytestmark = pytest.mark.integration

POLICY = {"min_calls": 5, "open_duration_s": 1, "max_open_duration_s": 4}


def _instance(url: str, sink: InMemoryTelemetrySink) -> CircuitBreaker:
    clock = SystemClock()
    store = RedisCircuitStore(url, clock=clock)
    return CircuitBreaker(
        store,
        clock=clock,
        sink=sink,
        publish=store.publish_change,
        listen=store.listen,
        refresh_interval_s=0.2,
    )


@pytest.mark.req("REQ-4.8-1", "REQ-4.8-6")
async def test_two_instances_see_one_circuit_and_send_exactly_one_probe(
    make_gateway: GatewayFactory,
) -> None:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.fail("REDIS_URL must point at a Redis server to run integration tests")
    api_id = f"cb_{uuid.uuid4().hex[:8]}"
    sink = InMemoryTelemetrySink()
    calls: list[int] = []

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        calls.append(1)
        if len(calls) > 5:
            await asyncio.sleep(
                0.7
            )  # a slow probe (attempt timeout is 1 s): the rest arrive meanwhile
        writer.write(_resp(503) if len(calls) <= 5 else OK)
        await writer.drain()
        writer.close()

    listener = await asyncio.start_server(upstream, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    gateways = []
    try:
        for _ in range(2):
            entry = api_entry(
                api_id, "none", {"mode": "none"}, port, retry={"max_attempts": 1}, circuit=POLICY
            )
            gateways.append(make_gateway(apis=[entry], circuit_breaker=_instance(url, sink)))
        a, b = gateways
        for _ in range(5):
            assert (await a.call("GET", "/gw/none/x")).status_code == 503
        await a.forwarder._circuit.drain()
        seen = await b.call("GET", "/gw/none/x")
        assert seen.json()["error"]["code"] == "JV_CIRCUIT_OPEN"  # B read the shared state
        assert len(calls) == 5

        await asyncio.sleep(1.2)  # open duration over
        responses = await asyncio.gather(
            *(gateways[n % 2].call("GET", "/gw/none/x") for n in range(50))
        )
    finally:
        listener.close()
        for gateway in gateways:
            await gateway.forwarder.aclose()
        client = redis_asyncio.from_url(url)
        for key in await client.keys(f"{PREFIX}{api_id}:*"):
            await client.delete(key)
        await client.aclose()

    ok = [r for r in responses if r.status_code == 200]
    open_ = [r for r in responses if r.status_code == 503]
    assert len(ok) == 1  # the probe
    assert len(open_) == 49
    assert len(calls) == 6  # five that tripped it plus exactly one probe
    transitions = {(e["from"], e["to"]) for e in sink.events if isinstance(e, dict)}
    assert {("CLOSED", "OPEN"), ("OPEN", "HALF_OPEN"), ("HALF_OPEN", "CLOSED")} <= transitions
