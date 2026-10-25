"""Sends an authenticated request to the upstream and streams the answer back (SPEC §4.3).

One pooled ``httpx.AsyncClient`` per upstream origin, each on ``PinnedTransport`` so every
connection is SSRF-checked. Single attempt: retries, circuit breaking and caching come later.
Error text is fixed: never an address, a URL or a secret.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import logging
import math
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from jarvis_core.circuit import is_failure
from jarvis_core.clock import Clock, Random, SystemClock, SystemRandom
from jarvis_core.deadline import Deadline
from jarvis_core.errors import (
    AuthInvalidError,
    BodyTooLargeError,
    CircuitOpenError,
    QuotaExceededError,
    RateLimitedError,
    UpstreamTimeoutError,
    UpstreamUnreachableError,
)
from jarvis_core.interfaces import CircuitState, RateDecision, RateLimiter
from jarvis_core.retry import IDEMPOTENT_METHODS, NO_RETRY, Failure, decide
from jarvis_core.ssrf import SsrfPolicy
from starlette.background import BackgroundTask
from starlette.types import Receive, Scope, Send

from jarvis_gateway.auth import Authenticated
from jarvis_gateway.circuit import CircuitBreaker, Ticket
from jarvis_gateway.config.snapshot import ApiEntry, Snapshot
from jarvis_gateway.credentials import CredentialUnavailable, CredentialVault
from jarvis_gateway.forward import (
    build_target_url,
    filter_request_headers,
    filter_response_headers,
    inject_credential_header,
)
from jarvis_gateway.record import RequestRecord
from jarvis_gateway.retry_budget import RetryBudget
from jarvis_gateway.upstream import PinnedTransport

log = logging.getLogger(__name__)

# httpx logs every request URL at INFO. For a ``query`` credential that URL holds the provider
# secret, so these loggers stay at WARNING whatever the operator sets for the root logger.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

Origin = tuple[str, str, int]


class _ProbeState:
    """Whether this request still owes the circuit a verdict on its half-open probe."""

    def __init__(self, pending: bool) -> None:
        self.pending = pending


class UpstreamBodyError(Exception):
    """The upstream failed after it had started to answer; the connection to the caller is cut."""


def _raw_headers(request: Request) -> list[tuple[str, str]]:
    return [(n.decode("latin-1"), v.decode("latin-1")) for n, v in request.headers.raw]


def _target_parts(request: Request, api_slug: str) -> tuple[str, str]:
    """The path after ``/gw/{slug}`` and the query string, exactly as the client sent them.

    The slug is the first raw segment. If it only matched the route because of an encoded ``/``
    in it (``weather%2Fv1``), the split the router used differs from this one: refuse.
    """
    raw = request.scope.get("raw_path")
    full = raw.decode("latin-1") if isinstance(raw, bytes) else request.url.path
    segment, slash, rest = full.removeprefix("/gw/").partition("/")
    if not full.startswith("/gw/") or unquote(segment) != api_slug:
        raise AuthInvalidError("invalid or missing JARVIS key")
    return slash + rest, request.scope.get("query_string", b"").decode("latin-1")


def _wire(headers: list[tuple[str, str]], injected_last: bool) -> list[tuple[bytes, bytes]]:
    """Header bytes as received (latin-1). The injected credential header (always last, when
    there is one) is sent as UTF-8, whatever characters the provider secret holds."""
    out = [(n.encode("latin-1"), v.encode("latin-1")) for n, v in headers]
    if injected_last and headers:
        name, value = headers[-1]
        out[-1] = (name.encode("utf-8"), value.encode("utf-8"))
    return out


class Forwarder:
    def __init__(
        self,
        policy: SsrfPolicy,
        vault: CredentialVault,
        *,
        max_body_bytes: int,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        clock: Clock | None = None,
        random: Random | None = None,
        retry_budget: RetryBudget | None = None,
        circuit: CircuitBreaker | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._vault = vault
        self._random = SystemRandom() if random is None else random
        self._budget = retry_budget
        self._circuit = circuit
        self._limiter = limiter
        self._clock = SystemClock() if clock is None else clock
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
        if self._budget is not None:
            await self._budget.aclose()
        if self._circuit is not None:
            await self._circuit.aclose()
        closer = getattr(self._limiter, "aclose", None)
        if closer is not None:
            await closer()
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

    async def _read_body(self, request: Request, deadline: Deadline) -> bytes:
        """The request body, within the deadline: a stalled upload is answered 504."""
        chunks: list[bytes] = []
        total = 0
        try:
            async with asyncio.timeout(deadline.remaining()):
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > self.max_body_bytes:
                        raise BodyTooLargeError("request body is too large")
                    chunks.append(chunk)
        except TimeoutError:
            raise UpstreamTimeoutError("the request was not completed in time") from None
        return b"".join(chunks)

    async def _take(self, key: str, capacity: int, refill_per_s: float) -> RateDecision | None:
        """One token from ``key``; None if the limiter itself breaks: fail open (SPEC D3)."""
        assert self._limiter is not None
        try:
            return await self._limiter.take(key, capacity=capacity, refill_per_s=refill_per_s)
        except Exception as exc:
            log.warning("rate limiter failed (%s); letting the request through", type(exc).__name__)
            return None

    async def check_inbound(self, authenticated: Authenticated) -> None:
        """Pipeline step 3: the (project, key, api) bucket, when the API turns it on."""
        policy = authenticated.api.config.rate_limits.inbound
        if self._limiter is None or not policy.enabled:
            return
        auth = authenticated.auth
        key = f"in:{auth.project_id}:{auth.key_id}:{authenticated.api.api_id}"
        decision = await self._take(key, policy.capacity, policy.refill_per_s)
        if decision is not None and not decision.allowed:
            limited = RateLimitedError("rate limit exceeded for this key")
            limited.retry_after_s = decision.retry_after_s
            raise limited

    async def _upstream_allows(self, api: ApiEntry) -> RateDecision | None:
        """Pipeline step 7: the per-API quota bucket. None when it is off or allows the call."""
        policy = api.config.rate_limits.upstream
        if self._limiter is None or not policy.enabled:
            return None
        decision = await self._take(f"up:{api.api_id}", policy.capacity, policy.refill_per_s)
        return decision if decision is not None and not decision.allowed else None

    async def forward(
        self,
        request: Request,
        authenticated: Authenticated,
        request_id: str,
        started: float,
        record: RequestRecord,
    ) -> Response:
        """Circuit check, then the attempts. ``started`` is the clock's ``monotonic()`` at ingress:
        the deadline runs from there."""
        api = authenticated.api
        config = api.config
        deadline = Deadline(self._clock, started, config.deadline_ms)
        ticket = Ticket("allow", CircuitState.CLOSED)
        if self._circuit is not None:
            ticket = await self._circuit.admit(
                api.api_id,
                config.circuit,
                api.project_id,
                probe_ttl_s=math.ceil(config.deadline_ms / 1000) + 1,
            )
            record.set_circuit(ticket.state.value)
            if ticket.action == "reject":
                rejected = CircuitOpenError("the circuit for this API is open")
                rejected.circuit_state = ticket.state.value
                raise rejected
        probe = _ProbeState(ticket.action == "probe")
        try:
            return await self._send(request, authenticated, request_id, deadline, record, probe)
        finally:
            if probe.pending and self._circuit is not None:  # cancelled or failed before a verdict
                await self._circuit.finish_probe(
                    api.api_id, config.circuit, api.project_id, "cancelled"
                )

    async def _send(
        self,
        request: Request,
        authenticated: Authenticated,
        request_id: str,
        deadline: Deadline,
        record: RequestRecord,
        probe: _ProbeState,
    ) -> Response:
        api = authenticated.api
        config = api.config
        # No await between the caller's snapshot read and this lookup: the secret is the one that
        # belongs to the policy that was just resolved.
        try:
            secret = self._vault.secret_for(api)
        except CredentialUnavailable:
            raise UpstreamUnreachableError("the upstream credential is not available") from None
        refused = await self._upstream_allows(api)  # pipeline step 7
        if refused is not None:
            exceeded = QuotaExceededError("the upstream quota for this API is used up")
            exceeded.retry_after_s = refused.retry_after_s
            raise exceeded
        path, query = _target_parts(request, api.slug)
        url = build_target_url(config.base_url, path, query, config.credential, secret)
        forwarded = filter_request_headers(authenticated.forward_headers(_raw_headers(request)))
        forwarded = _with_idempotency_key(
            forwarded, request.method, config.retry.idempotency_header
        )
        headers = inject_credential_header(forwarded, config.credential, secret)
        body = await self._read_body(request, deadline)
        record.add_request_bytes(len(body))
        client = self._client(httpx.URL(url))
        if self._budget is not None:
            self._budget.record_request(api.api_id)
        attempts = 0
        while True:
            if deadline.expired:
                raise UpstreamTimeoutError("the upstream did not answer in time")
            attempts += 1
            # One attempt, bounded by its own timeout and by what is left of the deadline (4.6).
            attempt_s = deadline.attempt_timeout(config.attempt_timeout_ms / 1000)
            upstream: httpx.Response | None = None
            error: Exception | None = None
            try:
                upstream_request = client.build_request(
                    request.method,
                    url,
                    headers=_wire(headers, config.credential.mode in ("bearer", "header")),
                    content=body or None,
                    timeout=httpx.Timeout(attempt_s),
                )
                record.mark_attempt()
                async with asyncio.timeout(attempt_s):
                    upstream = await client.send(upstream_request, stream=True)
                record.mark_headers_received()
            except (TimeoutError, httpx.TimeoutException):
                failure = Failure("timeout")
                error = UpstreamTimeoutError("the upstream did not answer in time")
            except httpx.TransportError:
                failure = Failure("connect")
                error = UpstreamUnreachableError("could not reach the upstream")
            except httpx.InvalidURL:  # deterministic: never retried
                raise UpstreamUnreachableError("could not reach the upstream") from None
            else:
                failure = Failure(
                    "status", upstream.status_code, upstream.headers.get("retry-after")
                )
            if self._circuit is not None:
                failed = is_failure(
                    status=upstream.status_code if upstream is not None else None,
                    timed_out=isinstance(error, UpstreamTimeoutError),
                    connect_error=isinstance(error, UpstreamUnreachableError),
                )
                if probe.pending:  # the first attempt of the probing request is the probe
                    probe.pending = False
                    await self._circuit.finish_probe(
                        api.api_id,
                        config.circuit,
                        api.project_id,
                        "failure" if failed else "success",
                    )
                else:
                    self._circuit.record(api.api_id, config.circuit, api.project_id, failure=failed)
            decision = decide(
                config.retry,
                method=request.method,
                failure=failure,
                attempts_made=attempts,
                deadline=deadline,
                min_attempt_ms=config.min_attempt_ms,
                rng=self._random,
                now_epoch=self._clock.now(),
            )
            if decision.retry and self._circuit is not None and self._circuit.is_open(api.api_id):
                decision = NO_RETRY  # the circuit opened meanwhile: stop hammering
            if decision.retry and await self._upstream_allows(api) is not None:
                decision = NO_RETRY  # a retry is a call leaving the gateway: it needs a token too
            if decision.retry and self._budget is not None:
                decision = (
                    decision
                    if await self._budget.try_acquire(
                        api.api_id,
                        config.retry.budget_ratio,
                        project_id=api.project_id,
                        request_id=request_id,
                    )
                    else NO_RETRY
                )
            if not decision.retry:
                if error is not None:
                    raise error from None
                assert upstream is not None
                return _stream_back(
                    upstream,
                    request_id,
                    record,
                    deadline,
                    attempts,
                    self._circuit.state(api.api_id).value if self._circuit is not None else None,
                )
            if upstream is not None:
                await upstream.aclose()
            await self._clock.sleep(decision.delay_s)


def _with_idempotency_key(
    headers: list[tuple[str, str]], method: str, header: str | None
) -> list[tuple[str, str]]:
    """Add one generated idempotency key for a POST/PATCH-like request (SPEC 4.7), reused on every
    attempt because the header list is built once. A key the caller already sent is kept."""
    if header is None or method.upper() in IDEMPOTENT_METHODS:
        return headers
    if any(name.lower() == header.lower() for name, _ in headers):
        return headers
    return [*headers, (header, uuid.uuid4().hex)]


def _origin(url: httpx.URL) -> Origin:
    port = url.port or (443 if url.scheme == "https" else 80)
    return url.scheme, url.host, port


class _RecordedStream(StreamingResponse):
    """Finishes the request's telemetry record however the response ends (done, cut, cancelled)."""

    def __init__(self, *args: Any, record: RequestRecord, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._record = record

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._record.finish(status=self.status_code, source="upstream", error_code=None)


def _stream_back(
    upstream: httpx.Response,
    request_id: str,
    record: RequestRecord,
    deadline: Deadline,
    attempts: int,
    circuit: str | None = None,
) -> Response:
    async def body() -> AsyncIterator[bytes]:
        chunks = upstream.aiter_raw().__aiter__()
        try:
            while True:
                try:
                    # The deadline covers the whole request, the body included (REQ-4.6-5).
                    async with asyncio.timeout(deadline.remaining()):
                        chunk = await anext(chunks)
                except StopAsyncIteration:
                    return
                record.add_response_bytes(len(chunk))
                yield chunk
        except TimeoutError:
            log.warning("the upstream response was cut at the request deadline")
            raise UpstreamBodyError("the upstream response exceeded the deadline") from None
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
        ("X-Jarvis-Attempts", str(attempts)),
    ]
    if circuit is not None:
        kept.append(("X-Jarvis-Circuit", circuit))
    response = _RecordedStream(
        body(),
        status_code=upstream.status_code,
        background=BackgroundTask(upstream.aclose),
        record=record,
    )
    response.raw_headers = [(n.lower().encode("latin-1"), v.encode("latin-1")) for n, v in kept]
    return response
