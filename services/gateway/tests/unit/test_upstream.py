"""PinnedBackend and PinnedTransport with a recording backend: no sockets, no DNS."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Iterable, Sequence
from typing import Any

import httpcore
import httpx
import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.errors import DestinationBlockedError
from jarvis_core.ssrf import DestinationResolver, SsrfPolicy
from jarvis_gateway.upstream import PinnedBackend, PinnedTransport

pytestmark = pytest.mark.unit

PUBLIC = "93.184.216.34"
PUBLIC_2 = "93.184.216.35"


class FakeStream(httpcore.AsyncNetworkStream):
    def __init__(self, backend: RecordingBackend, address: str) -> None:
        self.backend = backend
        self.address = address

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        raise httpcore.ReadError("fake stream")

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        raise httpcore.WriteError("fake stream")

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.backend.tls_hostnames.append(server_hostname)
        raise httpcore.ConnectError("stop after the handshake request")

    def get_extra_info(self, info: str) -> Any:
        return None


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """Records what it was asked to connect to; ``fail`` lists addresses that refuse."""

    def __init__(self, fail: Sequence[str] = ()) -> None:
        self.connected: list[tuple[str, int]] = []
        self.tls_hostnames: list[str | None] = []
        self.fail = set(fail)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connected.append((host, port))
        if host in self.fail:
            raise httpcore.ConnectError("refused")
        return FakeStream(self, host)

    async def sleep(self, seconds: float) -> None:
        return None


class Answers:
    def __init__(self, *rounds: dict[str, Sequence[str]]) -> None:
        self.rounds = list(rounds)
        self.calls = 0

    async def __call__(self, host: str, port: int) -> Sequence[str]:
        if port == 0:
            return []  # internal-host lookups
        round_ = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1
        return round_.get(host, [])


def _transport(
    answers: Answers, backend: RecordingBackend, policy: SsrfPolicy | None = None
) -> PinnedTransport:
    return PinnedTransport(
        policy or SsrfPolicy(), resolver=answers, clock=FakeClock(), backend=backend
    )


async def _get(transport: PinnedTransport, url: str) -> None:
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get(url)


@pytest.mark.req("REQ-4.13-2")
async def test_the_connection_goes_to_the_validated_ip_never_the_name() -> None:
    backend = RecordingBackend()
    transport = _transport(Answers({"api.example.com": [PUBLIC]}), backend)
    with pytest.raises(httpx.ConnectError):  # the fake stream stops at the TLS step
        await _get(transport, "https://api.example.com/v1/x")
    assert backend.connected == [(PUBLIC, 443)]


@pytest.mark.req("REQ-4.13-2")
async def test_sni_stays_the_original_host_name() -> None:
    backend = RecordingBackend()
    transport = _transport(Answers({"api.example.com": [PUBLIC]}), backend)
    with pytest.raises(httpx.ConnectError):
        await _get(transport, "https://api.example.com/")
    assert backend.tls_hostnames == ["api.example.com"]


@pytest.mark.req("REQ-4.13-3", "REQ-4.13-6")
async def test_a_private_address_is_refused_before_any_connection() -> None:
    backend = RecordingBackend()
    transport = _transport(Answers({"evil.example": ["10.0.0.5"]}), backend)
    with pytest.raises(DestinationBlockedError) as exc:
        await _get(transport, "https://evil.example/")
    assert exc.value.code == "JV_DESTINATION_BLOCKED"
    assert backend.connected == []


@pytest.mark.req("REQ-4.13-3")
async def test_a_name_with_a_public_and_a_private_address_is_refused() -> None:
    backend = RecordingBackend()
    transport = _transport(Answers({"mixed.example": [PUBLIC, "127.0.0.1"]}), backend)
    with pytest.raises(DestinationBlockedError):
        await _get(transport, "https://mixed.example/")
    assert backend.connected == []


async def test_the_next_validated_address_is_tried_when_one_refuses() -> None:
    backend = RecordingBackend(fail=[PUBLIC])
    transport = _transport(Answers({"two.example": [PUBLIC, PUBLIC_2]}), backend)
    with pytest.raises(httpx.ConnectError):  # second connect reaches the TLS stop
        await _get(transport, "https://two.example/")
    assert backend.connected == [(PUBLIC, 443), (PUBLIC_2, 443)]


async def test_when_every_address_refuses_the_error_is_a_connect_error() -> None:
    backend = RecordingBackend(fail=[PUBLIC])
    transport = _transport(Answers({"one.example": [PUBLIC]}), backend)
    with pytest.raises(httpx.ConnectError, match="refused"):
        await _get(transport, "https://one.example/")


async def test_an_unresolvable_host_is_a_connect_error_not_a_policy_block() -> None:
    backend = RecordingBackend()
    transport = _transport(Answers({}), backend)
    with pytest.raises(httpx.ConnectError, match="cannot resolve"):
        await _get(transport, "https://nx.example/")
    assert backend.connected == []


@pytest.mark.req("REQ-4.13-5")
async def test_plain_http_to_a_host_not_on_the_allowlist_is_refused_before_dns() -> None:
    backend = RecordingBackend()
    answers = Answers({"api.example.com": [PUBLIC]})
    with pytest.raises(DestinationBlockedError):
        await _get(_transport(answers, backend), "http://api.example.com/")
    assert answers.calls == 0
    assert backend.connected == []


@pytest.mark.req("REQ-4.13-4", "REQ-4.13-5")
async def test_an_allowlisted_internal_host_over_http_connects_to_its_private_address() -> None:
    backend = RecordingBackend()
    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "svc.internal"})
    transport = _transport(Answers({"svc.internal": ["10.1.2.3"]}), backend, policy)
    with pytest.raises(httpx.ReadError):  # plain http: the fake stream fails on write
        await _get(transport, "http://svc.internal:8080/")
    assert backend.connected == [("10.1.2.3", 8080)]


@pytest.mark.req("REQ-4.13-3")
@pytest.mark.parametrize(
    "url",
    ["https://127.0.0.1/", "https://2130706433/", "https://[::1]/", "https://169.254.169.254/x",
     "https://localhost/", "https://[::ffff:7f00:1]/"],
)  # fmt: skip
async def test_literal_and_alternate_spellings_never_reach_the_network(url: str) -> None:
    backend = RecordingBackend()
    with pytest.raises(DestinationBlockedError):
        await _get(_transport(Answers({}), backend), url)
    assert backend.connected == []


async def test_a_slow_resolver_hits_the_connect_timeout() -> None:
    async def slow(host: str, port: int) -> Sequence[str]:
        if port != 0:
            await asyncio.sleep(5)
        return []

    backend = PinnedBackend(
        DestinationResolver(SsrfPolicy(), slow, FakeClock()), RecordingBackend()
    )
    with pytest.raises(httpcore.ConnectTimeout):
        await backend.connect_tcp("slow.example", 443, timeout=0.05)


async def test_unix_sockets_are_refused_and_sleep_is_delegated() -> None:
    inner = RecordingBackend()
    backend = PinnedBackend(DestinationResolver(SsrfPolicy(), Answers({}), FakeClock()), inner)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_unix_socket("/var/run/docker.sock")
    await backend.sleep(0)
