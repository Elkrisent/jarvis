"""Sends an authenticated request to the upstream and streams the answer back (SPEC §4.3).

One pooled ``httpx.AsyncClient`` per upstream origin, each on ``PinnedTransport`` so every
connection is SSRF-checked. Single attempt: retries, circuit breaking and caching come later.
Error text is fixed: never an address, a URL or a secret.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import logging
from collections.abc import AsyncIterator, Callable

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from jarvis_core.errors import (
    BodyTooLargeError,
    UpstreamTimeoutError,
    UpstreamUnreachableError,
)
from jarvis_core.ssrf import SsrfPolicy
from starlette.background import BackgroundTask

from jarvis_gateway.auth import Authenticated
from jarvis_gateway.config.snapshot import Snapshot
from jarvis_gateway.credentials import CredentialUnavailable, CredentialVault
from jarvis_gateway.forward import (
    build_target_url,
    filter_request_headers,
    filter_response_headers,
    inject_credential_header,
)
from jarvis_gateway.upstream import PinnedTransport

log = logging.getLogger(__name__)

# httpx logs every request URL at INFO. For a ``query`` credential that URL holds the provider
# secret, so these loggers stay at WARNING whatever the operator sets for the root logger.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

Origin = tuple[str, str, int]


class UpstreamBodyError(Exception):
    """The upstream failed after it had started to answer; the connection to the caller is cut."""


def _raw_headers(request: Request) -> list[tuple[str, str]]:
    return [(n.decode("latin-1"), v.decode("latin-1")) for n, v in request.headers.raw]


def _target_parts(request: Request, api_slug: str) -> tuple[str, str]:
    """The path after ``/gw/{slug}`` and the query string, exactly as the client sent them."""
    raw = request.scope.get("raw_path")
    full = raw.decode("latin-1") if isinstance(raw, bytes) else request.url.path
    prefix = f"/gw/{api_slug}"
    path = full[len(prefix) :] if full.startswith(prefix) else ""
    return path, request.scope.get("query_string", b"").decode("latin-1")


class Forwarder:
    def __init__(
        self,
        policy: SsrfPolicy,
        vault: CredentialVault,
        *,
        max_body_bytes: int,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._vault = vault
        self.max_body_bytes = max_body_bytes
        self._factory = transport_factory or (lambda: PinnedTransport(policy))
        self._clients: dict[Origin, httpx.AsyncClient] = {}
        self._closing: set[asyncio.Task[None]] = set()

    def origins(self) -> list[Origin]:
        return list(self._clients)

    def retain(self, snapshot: Snapshot) -> None:
        """Drop the clients of origins that no API in ``snapshot`` uses (a store listener)."""
        used = {_origin(httpx.URL(a.config.base_url)) for a in snapshot.apis.values()}
        for origin in [o for o in self._clients if o not in used]:
            self._discard(self._clients.pop(origin))

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        await asyncio.gather(*(c.aclose() for c in clients), return_exceptions=True)
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)

    def _discard(self, client: httpx.AsyncClient) -> None:
        try:
            task = asyncio.get_running_loop().create_task(client.aclose())
        except RuntimeError:  # no loop here (a sync caller): the idle pool is simply dropped
            return
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def _client(self, url: httpx.URL) -> httpx.AsyncClient:
        origin = _origin(url)
        client = self._clients.get(origin)
        if client is None:
            client = httpx.AsyncClient(
                transport=self._factory(), follow_redirects=False, trust_env=False
            )
            client.headers.clear()  # httpx must not add User-Agent, Accept-Encoding and the like
            # Never keep cookies between requests: they belong to the caller, not the gateway.
            client.cookies.jar.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
            self._clients[origin] = client
        return client

    async def _read_body(self, request: Request) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > self.max_body_bytes:
                raise BodyTooLargeError("request body is too large")
            chunks.append(chunk)
        return b"".join(chunks)

    async def forward(
        self, request: Request, authenticated: Authenticated, request_id: str
    ) -> Response:
        api = authenticated.api
        config = api.config
        # No await between the caller's snapshot read and this lookup: the secret is the one that
        # belongs to the policy that was just resolved.
        try:
            secret = self._vault.secret_for(api)
        except CredentialUnavailable:
            raise UpstreamUnreachableError("the upstream credential is not available") from None
        path, query = _target_parts(request, api.slug)
        url = build_target_url(config.base_url, path, query, config.credential, secret)
        headers = inject_credential_header(
            filter_request_headers(authenticated.forward_headers(_raw_headers(request))),
            config.credential,
            secret,
        )
        body = await self._read_body(request)
        try:
            client = self._client(httpx.URL(url))
            upstream_request = client.build_request(
                request.method,
                url,
                headers=headers,
                content=body or None,
                timeout=httpx.Timeout(config.attempt_timeout_ms / 1000),
            )
            async with asyncio.timeout(config.deadline_ms / 1000):
                upstream = await client.send(upstream_request, stream=True)
        except (TimeoutError, httpx.TimeoutException):
            raise UpstreamTimeoutError("the upstream did not answer in time") from None
        except (httpx.TransportError, httpx.InvalidURL):
            raise UpstreamUnreachableError("could not reach the upstream") from None
        return _stream_back(upstream, request_id)


def _origin(url: httpx.URL) -> Origin:
    port = url.port or (443 if url.scheme == "https" else 80)
    return url.scheme, url.host, port


def _stream_back(upstream: httpx.Response, request_id: str) -> Response:
    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.HTTPError as exc:
            log.warning("the upstream response was interrupted (%s)", type(exc).__name__)
            raise UpstreamBodyError("the upstream response was interrupted") from None
        finally:
            await upstream.aclose()

    pairs = [(n.decode("latin-1"), v.decode("latin-1")) for n, v in upstream.headers.raw]
    kept = [
        (n, v) for n, v in filter_response_headers(pairs) if not n.lower().startswith("x-jarvis-")
    ]
    kept += [
        ("X-Jarvis-Request-Id", request_id),
        ("X-Jarvis-Source", "upstream"),
        ("X-Jarvis-Attempts", "1"),
    ]
    response = StreamingResponse(
        body(), status_code=upstream.status_code, background=BackgroundTask(upstream.aclose)
    )
    response.raw_headers = [(n.lower().encode("latin-1"), v.encode("latin-1")) for n, v in kept]
    return response
