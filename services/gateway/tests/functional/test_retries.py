"""M2.2: retries on real sockets (SPEC §4.7). A scripted upstream answers canned responses."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from rig import GatewayFactory

pytestmark = pytest.mark.functional

OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"


def _resp(status: int, reason: str = "X", extra: str = "", body: bytes = b"") -> bytes:
    head = (
        f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\n"
        f"{extra}Connection: close\r\n\r\n"
    )
    return head.encode() + body


class Scripted:
    """Answers request N with ``script[min(N, len-1)]`` and records every request head."""

    def __init__(self, *script: bytes) -> None:
        self.script = script
        self.heads: list[str] = []
        self.port = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        length = 0
        for line in head.split("\r\n")[1:]:
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1])
        if length:
            await reader.readexactly(length)
        answer = self.script[min(len(self.heads), len(self.script) - 1)]
        self.heads.append(head)
        writer.write(answer)
        await writer.drain()
        writer.close()

    def header(self, index: int, name: str) -> str | None:
        for line in self.heads[index].split("\r\n")[1:]:
            if line.lower().startswith(name.lower() + ":"):
                return line.split(":", 1)[1].strip()
        return None


@asynccontextmanager
async def scripted(*script: bytes) -> AsyncIterator[Scripted]:
    srv = Scripted(*script)
    listener = await asyncio.start_server(srv.handle, "127.0.0.1", 0)
    srv.port = listener.sockets[0].getsockname()[1]
    try:
        yield srv
    finally:
        listener.close()
        await listener.wait_closed()


def _gateway(make_gateway: GatewayFactory, port: int, **cfg: object):  # type: ignore[no-untyped-def]
    cfg.setdefault("retry", {"base_ms": 1, "cap_ms": 5})  # keep the tests fast
    return make_gateway(port=port, **cfg)


@pytest.mark.req("REQ-4.7-1", "REQ-4.4-3")
async def test_a_503_then_a_200_succeeds_in_two_attempts(make_gateway: GatewayFactory) -> None:
    async with scripted(_resp(503), OK) as upstream:
        response = await _gateway(make_gateway, upstream.port).call("GET", "/gw/none/x")
    assert response.status_code == 200
    assert response.text == "ok"
    assert response.headers["X-Jarvis-Attempts"] == "2"
    assert len(upstream.heads) == 2


@pytest.mark.req("REQ-4.7-9")
async def test_the_last_retryable_answer_is_passed_through_after_max_attempts(
    make_gateway: GatewayFactory,
) -> None:
    async with scripted(_resp(503, body=b"down")) as upstream:
        gateway = _gateway(
            make_gateway, upstream.port, retry={"base_ms": 1, "cap_ms": 5, "max_attempts": 3}
        )
        response = await gateway.call("GET", "/gw/none/x")
    assert response.status_code == 503
    assert response.text == "down"
    assert response.headers["X-Jarvis-Attempts"] == "3"
    assert len(upstream.heads) == 3


@pytest.mark.req("REQ-4.7-1")
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 500, 501])
async def test_other_statuses_are_never_retried(make_gateway: GatewayFactory, status: int) -> None:
    async with scripted(_resp(status), OK) as upstream:
        response = await _gateway(make_gateway, upstream.port).call("GET", "/gw/none/x")
    assert response.status_code == status
    assert response.headers["X-Jarvis-Attempts"] == "1"
    assert len(upstream.heads) == 1


@pytest.mark.req("REQ-4.7-3")
@pytest.mark.parametrize("method", ["POST", "PATCH"])
async def test_post_and_patch_without_idempotency_config_are_never_retried(
    make_gateway: GatewayFactory, method: str
) -> None:
    async with scripted(_resp(503), OK) as upstream:
        response = await _gateway(make_gateway, upstream.port).call(
            method, "/gw/none/x", content=b"{}"
        )
    assert response.status_code == 503
    assert len(upstream.heads) == 1  # the upstream saw exactly one call


@pytest.mark.req("REQ-4.7-3", "REQ-4.7-5")
async def test_post_with_idempotency_config_sends_the_same_generated_key_every_attempt(
    make_gateway: GatewayFactory,
) -> None:
    retry = {"base_ms": 1, "cap_ms": 5, "idempotency_header": "Idempotency-Key"}
    async with scripted(_resp(503), _resp(502), OK) as upstream:
        response = await _gateway(make_gateway, upstream.port, retry=retry).call(
            "POST", "/gw/none/x", content=b'{"a":1}'
        )
    assert response.status_code == 200
    assert response.headers["X-Jarvis-Attempts"] == "3"
    keys = [upstream.header(i, "Idempotency-Key") for i in range(3)]
    assert keys[0]
    assert keys == [keys[0]] * 3


@pytest.mark.req("REQ-4.7-5")
async def test_each_request_gets_its_own_key_and_a_callers_key_is_kept(
    make_gateway: GatewayFactory,
) -> None:
    retry = {"base_ms": 1, "cap_ms": 5, "idempotency_header": "Idempotency-Key"}
    async with scripted(OK) as upstream:
        gateway = _gateway(make_gateway, upstream.port, retry=retry)
        await gateway.call("POST", "/gw/none/x", content=b"1")
        await gateway.call("POST", "/gw/none/x", content=b"2")
        await gateway.call("POST", "/gw/none/x", content=b"3", headers={"Idempotency-Key": "mine"})
        await gateway.call("GET", "/gw/none/x")
    first, second = upstream.header(0, "Idempotency-Key"), upstream.header(1, "Idempotency-Key")
    assert first
    assert second
    assert first != second
    assert upstream.header(2, "Idempotency-Key") == "mine"
    assert upstream.header(3, "Idempotency-Key") is None  # safe methods get no generated key


@pytest.mark.req("REQ-4.7-2")
@pytest.mark.parametrize("status", [429, 503])
async def test_retry_after_is_honored_when_it_fits(
    make_gateway: GatewayFactory, status: int
) -> None:
    async with scripted(_resp(status, extra="Retry-After: 1\r\n"), OK) as upstream:
        started = time.monotonic()
        response = await _gateway(make_gateway, upstream.port, deadline_ms=5000).call(
            "GET", "/gw/none/x"
        )
        elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert 1.0 <= elapsed < 1.5


@pytest.mark.req("REQ-4.7-2")
@pytest.mark.parametrize("status", [429, 503])
async def test_a_retry_after_beyond_the_deadline_is_not_retried_and_not_waited_for(
    make_gateway: GatewayFactory, status: int
) -> None:
    async with scripted(_resp(status, extra="Retry-After: 30\r\n"), OK) as upstream:
        started = time.monotonic()
        response = await _gateway(make_gateway, upstream.port, deadline_ms=2000).call(
            "GET", "/gw/none/x"
        )
        elapsed = time.monotonic() - started
    assert response.status_code == status
    assert response.headers["X-Jarvis-Attempts"] == "1"
    assert elapsed < 0.5
    assert len(upstream.heads) == 1


@pytest.mark.req("REQ-4.7-1")
async def test_a_connection_error_is_retried_and_the_error_survives_if_it_keeps_failing(
    make_gateway: GatewayFactory,
) -> None:
    from rig import free_port

    response = await _gateway(make_gateway, free_port()).call("GET", "/gw/none/x")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "JV_UPSTREAM_UNREACHABLE"


@pytest.mark.req("REQ-4.7-1", "REQ-4.6-5")
async def test_an_attempt_timeout_is_retried_within_the_deadline(
    make_gateway: GatewayFactory,
) -> None:
    seen: list[int] = []

    async def first_hangs_then_ok(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        seen.append(1)
        if len(seen) == 1:
            await asyncio.sleep(30)
        writer.write(OK)
        await writer.drain()
        writer.close()

    listener = await asyncio.start_server(first_hangs_then_ok, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        gateway = _gateway(
            make_gateway, port, deadline_ms=3000, attempt_timeout_ms=300, min_attempt_ms=100
        )
        started = time.monotonic()
        response = await gateway.call("GET", "/gw/none/x")
        elapsed = time.monotonic() - started
    finally:
        listener.close()
    assert response.status_code == 200
    assert response.headers["X-Jarvis-Attempts"] == "2"
    assert 0.3 <= elapsed < 1.0


@pytest.mark.req("REQ-4.7-9", "REQ-4.6-5")
async def test_retries_never_push_a_request_past_its_deadline(make_gateway: GatewayFactory) -> None:
    async with scripted(_resp(503)) as upstream:
        gateway = _gateway(
            make_gateway,
            upstream.port,
            deadline_ms=400,
            attempt_timeout_ms=300,
            min_attempt_ms=100,
            retry={"base_ms": 150, "cap_ms": 150, "max_attempts": 10},
        )
        started = time.monotonic()
        response = await gateway.call("GET", "/gw/none/x")
        elapsed = time.monotonic() - started
    assert response.status_code == 503
    assert elapsed <= 0.4 + 0.05
    assert len(upstream.heads) < 10
