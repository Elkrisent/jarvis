"""M2.1: the per-request deadline and the per-attempt timeout hold on real sockets (SPEC §4.6)."""

from __future__ import annotations

import asyncio
import time

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jarvis_gateway.forwarder import UpstreamBodyError
from rig import GatewayFactory, Upstream, raw_exchange, serve

pytestmark = pytest.mark.functional

EPSILON = 0.05  # SPEC/IMPLEMENTATION M2.1: total latency <= deadline + 50 ms


@pytest.mark.req("REQ-4.6-2")
@pytest.mark.req("REQ-4.6-5")
async def test_a_hung_upstream_is_cut_at_the_attempt_timeout(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    gateway = make_gateway(
        deadline_ms=2000, attempt_timeout_ms=400, min_attempt_ms=100, retry={"max_attempts": 1}
    )
    await upstream.rules({"path_prefix": "/", "hang": True})
    started = time.monotonic()
    response = await gateway.call("GET", "/gw/none/x")
    elapsed = time.monotonic() - started
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "JV_UPSTREAM_TIMEOUT"
    assert 0.4 <= elapsed <= 0.4 + EPSILON


@pytest.mark.req("REQ-4.6-5")
async def test_time_already_spent_before_forwarding_shortens_the_attempt(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    """The deadline runs from ingress: with 500 ms gone, an attempt gets what is left, 100 ms."""

    gateway = make_gateway(deadline_ms=600, attempt_timeout_ms=500, min_attempt_ms=50)
    await upstream.rules({"path_prefix": "/", "hang": True})
    async with serve(gateway.app) as port:
        started = time.monotonic()
        # A slow client: the head arrives at once, the 500 ms of body does not.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /gw/none/x HTTP/1.1\r\nHost: g\r\nX-Jarvis-Key: jv_live_functionalkey01\r\n"
            b"Content-Length: 4\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        await asyncio.sleep(0.5)
        writer.write(b"abcd")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(-1), timeout=5)
        elapsed = time.monotonic() - started
        writer.close()
    assert data.startswith(b"HTTP/1.1 504"), data[:80]
    assert b"JV_UPSTREAM_TIMEOUT" in data
    assert elapsed <= 0.6 + EPSILON


@pytest.mark.req("REQ-4.6-5")
async def test_a_client_that_never_finishes_its_upload_is_answered_at_the_deadline(
    make_gateway: GatewayFactory,
) -> None:
    gateway = make_gateway(deadline_ms=500, attempt_timeout_ms=400, min_attempt_ms=100)
    async with serve(gateway.app) as port:
        started = time.monotonic()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /gw/none/x HTTP/1.1\r\nHost: g\r\nX-Jarvis-Key: jv_live_functionalkey01\r\n"
            b"Content-Length: 100\r\nConnection: close\r\n\r\nabc"  # 97 bytes never come
        )
        await writer.drain()
        data = await asyncio.wait_for(reader.read(-1), timeout=5)
        elapsed = time.monotonic() - started
        writer.close()
    assert data.startswith(b"HTTP/1.1 504"), data[:80]
    assert elapsed <= 0.5 + EPSILON


@pytest.mark.req("REQ-4.6-5")
async def test_a_body_that_trickles_past_the_deadline_is_cut(
    make_gateway: GatewayFactory,
) -> None:
    """Headers arrive in time, then one byte every 100 ms: the response is cut at the deadline."""

    async def trickle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n")
        try:
            for _ in range(100):
                writer.write(b"x")
                await writer.drain()
                await asyncio.sleep(0.1)
        except ConnectionError:
            pass
        finally:
            writer.close()

    listener = await asyncio.start_server(trickle, "127.0.0.1", 0)
    target = listener.sockets[0].getsockname()[1]
    try:
        gateway = make_gateway(
            port=target, deadline_ms=600, attempt_timeout_ms=500, min_attempt_ms=100
        )
        async with serve(gateway.app) as port:
            started = time.monotonic()
            request = (
                b"GET /gw/none/x HTTP/1.1\r\nHost: g\r\nX-Jarvis-Key: jv_live_functionalkey01\r\n"
                b"Connection: close\r\n\r\n"
            )
            status, _, body = await raw_exchange(port, request)
            elapsed = time.monotonic() - started
    finally:
        listener.close()
        await listener.wait_closed()
    assert status.startswith("HTTP/1.1 200")
    assert len(body) < 1000  # cut short, not completed
    assert elapsed <= 0.6 + 0.1  # one trickle step of slack for the socket close


@pytest.mark.req("REQ-4.6-5")
async def test_no_request_outlives_its_deadline_for_any_latency_sequence(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    """Hypothesis draws (deadline, attempt timeout, upstream latency); every call ends within its
    deadline plus a small epsilon: a 200 if the upstream was fast enough, else a 504 or a cut."""
    loop = asyncio.get_running_loop()
    gateways: dict[tuple[int, int], object] = {}

    async def one(deadline_ms: int, attempt_ms: int, latency_ms: int) -> None:
        key = (deadline_ms, attempt_ms)
        if key not in gateways:
            gateways[key] = make_gateway(
                deadline_ms=deadline_ms, attempt_timeout_ms=attempt_ms, min_attempt_ms=10
            )
        gateway = gateways[key]
        await upstream.rules({"path_prefix": "/", "latency_ms": latency_ms})
        started = time.monotonic()
        try:
            response = await gateway.call("GET", "/gw/none/x")  # type: ignore[attr-defined]
            status = response.status_code
        except UpstreamBodyError:
            status = 0  # headers made it, the body ran into the deadline: the response was cut
        elapsed = time.monotonic() - started
        assert elapsed <= deadline_ms / 1000 + 0.1, (deadline_ms, attempt_ms, latency_ms, elapsed)
        assert status in (0, 200, 504)
        if latency_ms + 60 < attempt_ms:  # comfortably fast
            assert status == 200
        if latency_ms > attempt_ms + 60:  # comfortably slow
            assert status == 504

    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=list(HealthCheck),
        derandomize=True,
    )
    @given(
        deadline_ms=st.sampled_from([150, 300, 450]),
        attempt_frac=st.sampled_from([0.4, 0.7, 1.0]),
        latency_ms=st.integers(min_value=0, max_value=600),
    )
    def search(deadline_ms: int, attempt_frac: float, latency_ms: int) -> None:
        asyncio.run_coroutine_threadsafe(
            one(deadline_ms, int(deadline_ms * attempt_frac), latency_ms), loop
        ).result(timeout=10)

    await asyncio.to_thread(search)
