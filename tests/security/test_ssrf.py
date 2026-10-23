"""SSRF bypass attempts (REQ-17-2, REQ-4.13-3, REQ-4.13-6). Kept permanently as regressions.

No Docker needed: names resolve through stubs and connections go to a recording backend or to
a local server. Every attempt is tried at save time (``DestinationChecker``) and at connect time
(``PinnedTransport``); each blocked attempt is ``JV_DESTINATION_BLOCKED`` / 403.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any

import httpcore
import httpx
import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.errors import DestinationBlockedError
from jarvis_core.ssrf import DestinationChecker, DestinationResolver, SsrfPolicy
from jarvis_gateway.upstream import PinnedTransport

pytestmark = pytest.mark.security

PUBLIC = "93.184.216.34"

NAMES = {
    "metadata.example": ["169.254.169.254"],
    "internal.example": ["10.0.0.5"],
    "loop.example": ["127.0.0.1"],
    "mixed.example": [PUBLIC, "192.168.1.10"],
    "v6loop.example": ["::1"],
    "mapped.example": ["::ffff:127.0.0.1"],
    "ula.example": ["fd00:ec2::254"],
    "cgnat.example": ["100.64.0.1"],
    "multicast.example": ["224.0.0.1"],
}

LITERAL_HOSTS = [
    "127.0.0.1", "127.1", "2130706433", "0177.0.0.1", "0x7f.0.0.1", "0x7f000001", "0",
    "0.0.0.0", "[::1]", "[::]", "[::ffff:127.0.0.1]", "[::ffff:7f00:1]", "[2002:7f00:1::]",
    "[64:ff9b::7f00:1]", "169.254.169.254", "0xa9.0xfe.0xa9.0xfe", "2852039166", "10.0.0.1",
    "172.16.0.1", "192.168.0.1", "100.64.0.1", "224.0.0.1", "255.255.255.255", "[fd00:ec2::254]",
    "[fe80::1]", "[ff02::1]", "localhost", "LOCALHOST", "localhost.", "foo.localhost",
    "ⓛⓞⓒⓐⓛⓗⓞⓢⓣ",  # circled letters fold to localhost
    "127。0。0。1",  # ideographic full stops fold to dots
    "１２７.０.０.１",  # full-width digits fold to 127.0.0.1  # noqa: RUF001
    "gateway", "api", "postgres", "redis", "worker",
]  # fmt: skip
NAME_HOSTS = list(NAMES)
ALL_HOSTS = LITERAL_HOSTS + NAME_HOSTS


async def _resolve(host: str, port: int) -> Sequence[str]:
    if port == 0:
        return {"api": ["172.18.0.3"], "postgres": ["172.18.0.2"]}.get(host, [])
    return NAMES.get(host, [])


def _destinations(policy: SsrfPolicy | None = None) -> DestinationResolver:
    return DestinationResolver(policy or SsrfPolicy(), _resolve, FakeClock())


class Recorder(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connected: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connected.append(host)
        raise httpcore.ConnectError("recorder")

    async def sleep(self, seconds: float) -> None:
        return None


def _assert_blocked(exc: DestinationBlockedError) -> None:
    assert exc.code == "JV_DESTINATION_BLOCKED"
    assert exc.http_status == 403


@pytest.mark.req("REQ-17-2", "REQ-4.13-3", "REQ-4.13-6")
@pytest.mark.parametrize("host", ALL_HOSTS)
async def test_save_rejects_every_bypass_form(host: str) -> None:
    checker = DestinationChecker(_destinations())
    with pytest.raises(DestinationBlockedError) as exc:
        await checker.check_save(f"https://{host}/v1")
    _assert_blocked(exc.value)


@pytest.mark.req("REQ-17-2", "REQ-4.13-3", "REQ-4.13-6")
@pytest.mark.parametrize("host", ALL_HOSTS)
async def test_connect_never_reaches_the_network_for_a_bypass_form(host: str) -> None:
    backend = Recorder()
    transport = PinnedTransport(SsrfPolicy(), resolver=_resolve, clock=FakeClock(), backend=backend)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises((DestinationBlockedError, httpx.InvalidURL)) as exc:
            await client.get(f"https://{host}/v1")
    if isinstance(exc.value, DestinationBlockedError):
        _assert_blocked(exc.value)
    assert backend.connected == []


@pytest.mark.req("REQ-17-2", "REQ-4.13-5", "REQ-4.13-6")
@pytest.mark.parametrize(
    "url",
    [
        "http://203.0.113.9/",
        "http://api.example.com/",
        "ftp://api.example.com/",
        "file:///etc/passwd",
        "gopher://api.example.com/",
        "https://good.example@127.0.0.1/",
        "https://good.example\\@127.0.0.1/",
        "https://good.example%00.evil@127.0.0.1/",
    ],
)
async def test_scheme_and_url_tricks_are_blocked_at_save(url: str) -> None:
    checker = DestinationChecker(_destinations())
    with pytest.raises(DestinationBlockedError) as exc:
        await checker.check_save(url)
    _assert_blocked(exc.value)


@pytest.mark.req("REQ-17-2", "REQ-4.13-3")
async def test_a_wide_allowlist_does_not_unblock_the_jarvis_containers() -> None:
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "0.0.0.0/0, ::/0, api, postgres"})
    checker = DestinationChecker(_destinations(policy))
    for host in ("api", "postgres", "localhost"):
        with pytest.raises(DestinationBlockedError):
            await checker.check_save(f"https://{host}/")
    NAMES["by-ip.example"] = ["172.18.0.3"]  # resolves to the same address as `api`
    try:
        with pytest.raises(DestinationBlockedError):
            await checker.check_save("https://by-ip.example/")
    finally:
        del NAMES["by-ip.example"]


@pytest.mark.req("REQ-17-2", "REQ-4.13-1", "REQ-4.13-2")
async def test_dns_rebinding_is_blocked_at_connect() -> None:
    answers = iter([[PUBLIC], ["169.254.169.254"]])

    async def rebinding(host: str, port: int) -> Sequence[str]:
        return [] if port == 0 else next(answers)

    policy = SsrfPolicy()
    await DestinationChecker(DestinationResolver(policy, rebinding, FakeClock())).check_save(
        "https://rebind.example/"
    )
    backend = Recorder()
    transport = PinnedTransport(policy, resolver=rebinding, clock=FakeClock(), backend=backend)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(DestinationBlockedError) as exc:
            await client.get("https://rebind.example/")
    _assert_blocked(exc.value)
    assert backend.connected == []


@pytest.fixture
async def redirecting_server() -> AsyncIterator[int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 302 Found\r\nLocation: https://169.254.169.254/latest/meta-data/\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    listener = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield int(listener.sockets[0].getsockname()[1])
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.req("REQ-17-2", "REQ-4.13-7")
async def test_a_redirect_to_the_metadata_address_is_blocked_when_redirects_are_followed(
    redirecting_server: int,
) -> None:
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "upstream.test"})

    async def resolve(host: str, port: int) -> Sequence[str]:
        return [] if port == 0 else ["127.0.0.1"]

    transport = PinnedTransport(policy, resolver=resolve, clock=FakeClock())
    url = f"http://upstream.test:{redirecting_server}/"
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(DestinationBlockedError) as exc:
            await client.get(url)
    _assert_blocked(exc.value)
    async with httpx.AsyncClient(transport=transport) as client:  # default: not followed
        assert (await client.get(url)).status_code == 302


@pytest.mark.req("REQ-17-2", "REQ-11.3-1")
async def test_errors_and_logs_never_contain_the_host_or_the_resolved_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    checker = DestinationChecker(
        _destinations(SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "10.9.0.0/16"}))
    )
    with caplog.at_level(logging.DEBUG):
        for host in ("metadata.example", "internal.example", "mixed.example", "169.254.169.254"):
            with pytest.raises(DestinationBlockedError) as exc:
                await checker.check_save(f"https://{host}/")
            assert str(exc.value) == "destination is not allowed"
            assert exc.value.to_body()["error"]["message"] == "destination is not allowed"
    rendered = "\n".join(r.getMessage() + repr(r.args) for r in caplog.records)
    for needle in ("metadata", "internal", "mixed", "169.254", "10.0.0.5", "192.168", "10.9.0"):
        assert needle not in rendered
