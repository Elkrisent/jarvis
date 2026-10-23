"""HTTP transport that validates and pins every upstream connection (SPEC §4.13).

``PinnedBackend`` resolves the host, checks every address against the SSRF policy and connects
to a validated address, all in one step, so the address that was checked is the address that is
connected (no DNS rebinding window). The request URL is untouched, so TLS SNI, certificate
verification and the ``Host`` header still use the name the client asked for. Every connection
goes through here, so each redirect hop is checked when redirects are followed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
from jarvis_core.clock import Clock
from jarvis_core.ssrf import (
    DestinationResolver,
    HostNotResolved,
    Resolver,
    SsrfPolicy,
    check_destination_name,
    normalize_host,
    system_resolver,
)

DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


class PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self, destinations: DestinationResolver, inner: httpcore.AsyncNetworkBackend | None = None
    ) -> None:
        self._destinations = destinations
        self._inner = httpcore.AnyIOBackend() if inner is None else inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        on_allowlist = self._destinations.policy.host_allowed(normalize_host(host))
        try:
            addresses = await asyncio.wait_for(
                self._destinations.resolve(host, port, on_allowlist=on_allowlist), timeout
            )
        except HostNotResolved:
            raise httpcore.ConnectError("cannot resolve the upstream host") from None
        except TimeoutError:
            raise httpcore.ConnectTimeout("resolving the upstream host timed out") from None
        last: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last = exc
        assert last is not None  # ``resolve`` returns at least one address
        raise last

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not allowed for upstreams")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """An ``httpx`` transport for upstream calls. No proxies, no unix sockets, no retries."""

    def __init__(
        self,
        policy: SsrfPolicy,
        *,
        resolver: Resolver = system_resolver,
        clock: Clock | None = None,
        backend: httpcore.AsyncNetworkBackend | None = None,
        verify: bool = True,
        limits: httpx.Limits = DEFAULT_LIMITS,
    ) -> None:
        # The base __init__ builds a pool with the default backend; ours replaces it.
        self._policy = policy
        destinations = DestinationResolver(policy, resolver, clock)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=verify),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=PinnedBackend(destinations, backend),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Scheme, host form and plain-http rules need no DNS: refuse early, before any lookup.
        check_destination_name(request.url.scheme, request.url.host, self._policy)
        return await super().handle_async_request(request)
