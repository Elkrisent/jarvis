"""Wire-level behaviour: the gateway under uvicorn, raw requests in, a raw upstream out."""

from __future__ import annotations

import gzip

import pytest
from rig import KEY, GatewayFactory, RawServer, api_entry, dechunk, raw_exchange, serve

pytestmark = pytest.mark.functional


def _gateway(make_gateway: GatewayFactory, raw: RawServer, mode: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    api = api_entry("raw-api", "raw", mode or {"mode": "none"}, raw.port)
    return make_gateway(apis=[api])


def _request(target: str, extra: str = "", method: str = "GET", body: bytes = b"") -> bytes:
    head = (
        f"{method} {target} HTTP/1.1\r\nHost: gateway\r\nX-Jarvis-Key: {KEY}\r\n"
        f"Connection: close\r\n{extra}\r\n"
    )
    return head.encode("latin-1") + body


@pytest.mark.req("REQ-4.1-1")
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/gw/raw/a/b?x=1", "/base/a/b?x=1"),
        ("/gw/raw/a%2Fb", "/base/a%2Fb"),
        ("/gw/raw/../../x", "/base/x"),
        ("/gw/raw/a/./b/../c", "/base/a/c"),
        ("/gw/raw/%2e%2e/%2E%2E/x", "/base/x"),
        ("/gw/raw//a///b", "/base/a/b"),
        ("/gw/raw/", "/base/"),
        ("/gw/raw", "/base"),
        ("/gw/raw/a?x=%20&&y", "/base/a?x=%20&&y"),
        ("/gw/raw/caf%C3%A9", "/base/caf%C3%A9"),
        ("/gw/raw/a;b=c", "/base/a;b=c"),
        ("/gw/raw/a?q=1&q=2", "/base/a?q=1&q=2"),
    ],
)
async def test_the_path_and_query_reach_the_upstream_as_the_client_sent_them(
    make_gateway: GatewayFactory, raw_server: RawServer, target: str, expected: str
) -> None:
    gateway = _gateway(make_gateway, raw_server)
    async with serve(gateway.app) as port:
        status, _, _ = await raw_exchange(port, _request(target))
    assert status.startswith("HTTP/1.1 200"), status
    assert raw_server.request_line == f"GET {expected} HTTP/1.1"


async def test_dot_segments_can_never_leave_the_apis_base_path(
    make_gateway: GatewayFactory, raw_server: RawServer
) -> None:
    gateway = _gateway(make_gateway, raw_server)
    async with serve(gateway.app) as port:
        for target in (
            "/gw/raw/../../../etc/passwd",
            "/gw/raw/%2e%2e%2f%2e%2e%2fetc",
            "/gw/raw/..",
        ):
            await raw_exchange(port, _request(target))
    for head in raw_server.heads:
        line = head.split("\r\n")[0].split(" ")[1]
        assert line.startswith("/base"), line
        assert ".." not in line


@pytest.mark.req("REQ-4.5-6", "REQ-4.5-7")
async def test_headers_are_forwarded_cleanly_and_the_key_stays_home(
    make_gateway: GatewayFactory, raw_server: RawServer
) -> None:
    gateway = _gateway(make_gateway, raw_server, {"mode": "bearer"})
    extra = (
        "Authorization: Bearer client-supplied\r\nAuthorization: Basic also-client\r\n"
        "X-Dup: one\r\nX-Dup: two\r\nCookie: a=1\r\nCookie: b=2\r\n"
        "X-Foo: dropped-by-connection\r\nUpgrade: websocket\r\nTE: trailers\r\n"
        "Proxy-Authorization: Basic x\r\nKeep-Alive: 5\r\nX-Keep: yes\r\n"
    )
    async with serve(gateway.app) as port:
        await raw_exchange(
            port,
            _request("/gw/raw/x", extra).replace(b"Connection: close", b"Connection: close, X-Foo"),
        )
    lines = raw_server.header_lines()
    names = [ln.split(":", 1)[0].lower() for ln in lines]
    assert names.count("x-dup") == 2
    assert [ln.lower() for ln in lines if ln.lower().startswith("x-dup")] == [
        "x-dup: one",
        "x-dup: two",
    ]
    assert [ln.lower() for ln in lines if ln.lower().startswith("cookie")] == [
        "cookie: a=1",
        "cookie: b=2",
    ]
    for gone in ("x-foo", "upgrade", "te", "proxy-authorization", "keep-alive", "x-jarvis-key"):
        assert gone not in names
    assert "user-agent" not in names
    assert "accept-encoding" not in names
    assert names.count("authorization") == 1
    assert KEY not in raw_server.heads[0]
    assert "client-supplied" not in raw_server.heads[0]
    assert "also-client" not in raw_server.heads[0]
    assert "x-keep: yes" in [ln.lower() for ln in lines]
    assert next(ln for ln in lines if ln.lower().startswith("host")).startswith(
        "Host: upstream.test:"
    )


@pytest.mark.req("REQ-4.1-1")
async def test_a_chunked_request_body_arrives_whole_with_a_content_length(
    make_gateway: GatewayFactory, raw_server: RawServer
) -> None:
    gateway = _gateway(make_gateway, raw_server)
    body = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    async with serve(gateway.app) as port:
        status, _, _ = await raw_exchange(
            port,
            _request("/gw/raw/up", "Transfer-Encoding: chunked\r\n", "POST", body),
        )
    assert status.startswith("HTTP/1.1 200"), status
    lines = [ln.lower() for ln in raw_server.header_lines()]
    assert "content-length: 11" in lines
    assert not any(ln.startswith("transfer-encoding") for ln in lines)
    assert raw_server.bodies[0] == b"hello world"


@pytest.mark.req("REQ-4.4-7")
async def test_a_gzip_response_is_returned_byte_for_byte_with_its_encoding(
    make_gateway: GatewayFactory, raw_server: RawServer
) -> None:
    payload = gzip.compress(b'{"hello": "world"}' * 50)
    raw_server.response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Encoding: gzip\r\n"
        b"Content-Length: %d\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2\r\nConnection: close\r\n\r\n"
        % len(payload)
        + payload
    )
    gateway = _gateway(make_gateway, raw_server)
    async with serve(gateway.app) as port:
        status, lines, body = await raw_exchange(port, _request("/gw/raw/z"))
    assert status.startswith("HTTP/1.1 200")
    assert body == payload
    lowered = [ln.lower() for ln in lines]
    assert "content-encoding: gzip" in lowered
    assert f"content-length: {len(payload)}" in lowered
    assert [ln.lower() for ln in lines if ln.lower().startswith("set-cookie")] == [
        "set-cookie: a=1",
        "set-cookie: b=2",
    ]
    assert not any(ln.startswith("connection: close, ") for ln in lowered)


async def test_cookies_set_by_the_upstream_are_not_replayed_to_the_next_caller(
    make_gateway: GatewayFactory, raw_server: RawServer
) -> None:
    raw_server.response = (
        b"HTTP/1.1 200 OK\r\nSet-Cookie: sid=secret-session\r\nContent-Length: 2\r\n"
        b"Connection: close\r\n\r\nok"
    )
    gateway = _gateway(make_gateway, raw_server)
    async with serve(gateway.app) as port:
        await raw_exchange(port, _request("/gw/raw/a"))
        await raw_exchange(port, _request("/gw/raw/b"))
    assert len(raw_server.heads) == 2
    assert "secret-session" not in raw_server.heads[1]
    assert "cookie:" not in raw_server.heads[1].lower()


async def test_a_chunked_upstream_response_reaches_the_caller_intact(
    make_gateway: GatewayFactory, raw_server: RawServer
) -> None:
    raw_server.response = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    )
    gateway = _gateway(make_gateway, raw_server)
    async with serve(gateway.app) as port:
        status, lines, body = await raw_exchange(port, _request("/gw/raw/c"))
    assert status.startswith("HTTP/1.1 200")
    assert dechunk(body) == b"hello world"
    assert any(ln.lower() == "transfer-encoding: chunked" for ln in lines)
