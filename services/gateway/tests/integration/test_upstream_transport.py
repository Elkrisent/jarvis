"""PinnedTransport against real local servers: Host header, DNS rebinding and redirects."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence

import httpx
import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.errors import DestinationBlockedError
from jarvis_core.ssrf import DestinationChecker, DestinationResolver, SsrfPolicy
from jarvis_gateway.upstream import PinnedTransport

pytestmark = pytest.mark.integration

PUBLIC = "93.184.216.34"


class Server:
    """A minimal HTTP/1.1 server on 127.0.0.1: records request heads, answers via ``respond``."""

    def __init__(self) -> None:
        self.heads: list[str] = []
        self.respond: Callable[[str], bytes] = lambda head: _ok(b"hello")
        self.port = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        self.heads.append(head)
        writer.write(self.respond(head))
        await writer.drain()
        writer.close()


def _ok(body: bytes) -> bytes:
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (
        len(body),
        body,
    )


def _redirect(location: str) -> bytes:
    return (
        f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n"
        "Connection: close\r\n\r\n"
    ).encode()


@pytest.fixture
async def server() -> AsyncIterator[Server]:
    srv = Server()
    listener = await asyncio.start_server(srv.handle, "127.0.0.1", 0)
    srv.port = listener.sockets[0].getsockname()[1]
    try:
        yield srv
    finally:
        listener.close()
        await listener.wait_closed()


def _resolver(mapping: dict[str, Sequence[str]]) -> Callable[[str, int], object]:
    async def resolve(host: str, port: int) -> Sequence[str]:
        return [] if port == 0 else mapping.get(host, [])

    return resolve


def _client(
    policy: SsrfPolicy, mapping: dict[str, Sequence[str]], *, follow: bool = False
) -> httpx.AsyncClient:
    transport = PinnedTransport(policy, resolver=_resolver(mapping), clock=FakeClock())  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, follow_redirects=follow)


@pytest.mark.req("REQ-4.13-4", "REQ-4.13-5")
async def test_an_allowlisted_internal_host_works_and_keeps_its_host_header(server: Server) -> None:
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "upstream.test, 127.0.0.1/32"})
    async with _client(policy, {"upstream.test": ["127.0.0.1"]}) as client:
        response = await client.get(f"http://upstream.test:{server.port}/v1/ping?x=1")
    assert response.status_code == 200
    assert response.text == "hello"
    assert f"Host: upstream.test:{server.port}" in server.heads[0]
    assert server.heads[0].startswith("GET /v1/ping?x=1 HTTP/1.1")


@pytest.mark.req("REQ-4.13-6")
async def test_without_the_allowlist_the_same_request_is_blocked(server: Server) -> None:
    async with _client(SsrfPolicy(), {"upstream.test": ["127.0.0.1"]}) as client:
        with pytest.raises(DestinationBlockedError):
            await client.get(f"http://upstream.test:{server.port}/")
    assert server.heads == []


@pytest.mark.req("REQ-4.13-4")
async def test_an_allowlisted_cidr_permits_a_name_that_resolves_into_it_over_https_rules(
    server: Server,
) -> None:
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "127.0.0.0/8, upstream.test"})
    async with _client(policy, {"upstream.test": ["127.0.0.1"]}) as client:
        assert (await client.get(f"http://upstream.test:{server.port}/")).status_code == 200


@pytest.mark.req("REQ-4.13-1", "REQ-4.13-2")
async def test_dns_rebinding_public_at_save_private_at_connect_is_blocked(server: Server) -> None:
    rounds = iter([[PUBLIC], ["127.0.0.1"], ["127.0.0.1"]])

    async def rebinding(host: str, port: int) -> Sequence[str]:
        return [] if port == 0 else next(rounds)

    policy = SsrfPolicy()
    checker = DestinationChecker(DestinationResolver(policy, rebinding, FakeClock()))
    await checker.check_save("https://rebind.example/")  # save time: public, accepted
    transport = PinnedTransport(policy, resolver=rebinding, clock=FakeClock())
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(DestinationBlockedError):
            await client.get(f"https://rebind.example:{server.port}/")
    assert server.heads == []


@pytest.mark.req("REQ-4.13-7")
async def test_redirects_are_not_followed_by_default(server: Server) -> None:
    server.respond = lambda head: _redirect("http://169.254.169.254/latest/meta-data/")
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "upstream.test, 127.0.0.1/32"})
    async with _client(policy, {"upstream.test": ["127.0.0.1"]}) as client:
        response = await client.get(f"http://upstream.test:{server.port}/")
    assert response.status_code == 302
    assert len(server.heads) == 1


@pytest.mark.req("REQ-4.13-7")
@pytest.mark.parametrize(
    "location",
    [
        "http://169.254.169.254/latest/meta-data/",
        "https://169.254.169.254/latest/meta-data/",
        "http://127.0.0.2:1/",  # loopback, but not the one allowlisted address
        "https://[::1]/",
        "http://2130706434/",  # 127.0.0.2 spelled as a decimal number
        "http://not-allowlisted.example/",
    ],
)
async def test_every_redirect_hop_is_checked(server: Server, location: str) -> None:
    server.respond = lambda head: _redirect(location)
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "upstream.test, 127.0.0.1/32"})
    mapping = {"upstream.test": ["127.0.0.1"], "not-allowlisted.example": [PUBLIC]}
    async with _client(policy, mapping, follow=True) as client:
        with pytest.raises(DestinationBlockedError):
            await client.get(f"http://upstream.test:{server.port}/")
    assert len(server.heads) == 1  # only the first hop reached a server


@pytest.mark.req("REQ-4.13-7")
async def test_a_redirect_to_an_allowlisted_host_is_followed(server: Server) -> None:
    server.respond = lambda head: (
        _ok(b"final") if "/final" in head.split("\r\n")[0] else _redirect("/final")
    )
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "upstream.test, 127.0.0.1/32"})
    async with _client(policy, {"upstream.test": ["127.0.0.1"]}, follow=True) as client:
        response = await client.get(f"http://upstream.test:{server.port}/start")
    assert response.text == "final"
    assert len(server.heads) == 2


@pytest.mark.req("REQ-4.13-3")
async def test_a_wide_allowlist_does_not_open_loopback_to_a_local_server(server: Server) -> None:
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "0.0.0.0/0, ::/0, upstream.test"})
    async with _client(policy, {"upstream.test": ["127.0.0.1"]}) as client:
        for url in (
            f"http://upstream.test:{server.port}/",
            f"http://127.0.0.1:{server.port}/",
            f"http://0.0.0.0:{server.port}/",
        ):
            with pytest.raises(DestinationBlockedError):
                await client.get(url)
    assert server.heads == []
