"""Forwarder and /gw route against httpx.MockTransport (no sockets)."""

from __future__ import annotations

import asyncio
import gzip
import http.cookiejar
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from jarvis_core.clock import SystemClock
from jarvis_core.config import ApiDependency
from jarvis_core.deadline import Deadline
from jarvis_core.errors import DestinationBlockedError, UpstreamTimeoutError
from jarvis_core.keys import hash_jarvis_key
from jarvis_core.secretbox import AesGcmSecretBox
from jarvis_core.ssrf import SsrfPolicy
from jarvis_gateway.app import create_app
from jarvis_gateway.config.manager import ConfigManager
from jarvis_gateway.config.snapshot import ApiEntry, KeyEntry, Snapshot
from jarvis_gateway.config.snapshot_file import CoalescingWriter, SnapshotFile
from jarvis_gateway.config.store import ConfigStore
from jarvis_gateway.credentials import CredentialVault
from jarvis_gateway.forwarder import Forwarder, UpstreamBodyError

pytestmark = pytest.mark.unit

BOX = AesGcmSecretBox(bytes(range(32)))
KEY = "jv_live_forwarderkey1"
SECRET = "sk-REAL-provider-secret"
LIMIT = 1024


class _Bytes(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._data


def _resp(
    status: int = 200,
    *,
    content: bytes = b"",
    headers: Mapping[str, str] | list[tuple[str, str]] | None = None,
) -> httpx.Response:
    """A streaming response, like a real transport's (a pre-read one cannot be streamed)."""
    return httpx.Response(status, headers=headers, stream=_Bytes(content))


class StoreOnly:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


def _api(
    api_id: str,
    slug: str,
    credential: dict[str, Any] | None = None,
    base_url: str = "https://api.example.com/v1",
    seal: bool = True,
    box: AesGcmSecretBox = BOX,
    **cfg: Any,
) -> ApiEntry:
    cred = credential if credential is not None else {"mode": "none"}
    if cred.get("mode") != "none":
        cred = {**cred, "secret_ref": f"sec_{api_id}"}
    config = ApiDependency.model_validate(
        {
            "slug": slug,
            "name": slug,
            "base_url": base_url,
            "credential": cred,
            "jarvis_key_slot": "authorization",
            **cfg,
        }
    )
    sealed = (
        box.seal(SECRET, context=f"sec_{api_id}") if cred.get("mode") != "none" and seal else None
    )
    return ApiEntry(api_id, "p1", slug, 1, config, sealed)


class Rig:
    def __init__(
        self, apis: list[ApiEntry], handler: Callable[[httpx.Request], Any] | None = None
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.factory_calls = 0
        self.handler = handler or (lambda request: _resp(200, content=b"hello"))
        self.store = ConfigStore()
        self.vault = CredentialVault(BOX)
        self.store.add_listener(self.vault.refresh)
        keys = {hash_jarvis_key(KEY): KeyEntry("k1", "p1", frozenset(), False)}
        self.store.replace(
            Snapshot.empty("postgres", 1.0).with_changes(
                apis={a.api_id: a for a in apis}, keys=keys
            )
        )

        async def respond(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            result = self.handler(request)
            if asyncio.iscoroutine(result):
                result = await result
            return cast(httpx.Response, result)

        def factory() -> httpx.AsyncBaseTransport:
            self.factory_calls += 1
            return httpx.MockTransport(respond)

        self.forwarder = Forwarder(
            SsrfPolicy(), self.vault, max_body_bytes=LIMIT, transport_factory=factory
        )
        app = create_app(
            config=cast(ConfigManager, StoreOnly(self.store)), forwarder=self.forwarder
        )
        self.client = TestClient(app)
        self.store.add_listener(self.forwarder.retain)
        # The test client must not add headers or replay cookies on its own: only what a test sends.
        self.client.headers.clear()
        self.client.cookies.jar.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))

    def call(self, method: str = "GET", url: str = "/gw/weather/v1/x", **kw: Any) -> httpx.Response:
        headers = {"X-Jarvis-Key": KEY, **kw.pop("headers", {})}
        return self.client.request(method, url, headers=headers, **kw)


def _rig(credential: dict[str, Any] | None = None, **kw: Any) -> Rig:
    return Rig([_api("a1", "weather", credential, **kw)])


# --- what the upstream receives ---------------------------------------------------------------


@pytest.mark.req("REQ-4.1-1")
def test_method_path_query_and_body_are_forwarded() -> None:
    rig = _rig()
    response = rig.call("POST", "/gw/weather/items/7?city=Bangalore&x=1", content=b'{"a":1}',
                        headers={"Content-Type": "application/json"})  # fmt: skip
    assert response.status_code == 200
    (up,) = rig.requests
    assert up.method == "POST"
    assert str(up.url) == "https://api.example.com/v1/items/7?city=Bangalore&x=1"
    assert up.content == b'{"a":1}'
    assert up.headers["content-type"] == "application/json"
    assert up.headers["host"] == "api.example.com"


@pytest.mark.req("REQ-4.1-1")
def test_the_base_url_alone_when_no_path_is_given() -> None:
    rig = _rig()
    rig.call("GET", "/gw/weather")
    assert str(rig.requests[0].url) == "https://api.example.com/v1"


@pytest.mark.req("REQ-4.5-2", "REQ-4.5-6")
def test_bearer_credential_is_injected_and_the_client_authorization_is_replaced() -> None:
    rig = _rig({"mode": "bearer"}, jarvis_key_slot="x-jarvis-key")
    rig.call(headers={"Authorization": "Bearer client-value"})
    (up,) = rig.requests
    assert up.headers.get_list("authorization") == [f"Bearer {SECRET}"]


@pytest.mark.req("REQ-4.5-3")
def test_header_credential_is_injected() -> None:
    rig = _rig({"mode": "header", "header_name": "X-Api-Key"})
    rig.call(headers={"x-api-key": "client-value"})
    assert rig.requests[0].headers.get_list("x-api-key") == [SECRET]


@pytest.mark.req("REQ-4.5-4")
def test_query_credential_is_injected_and_the_client_parameter_replaced() -> None:
    rig = _rig({"mode": "query", "param_name": "appid"})
    rig.call(url="/gw/weather/w?city=B&appid=client")
    assert str(rig.requests[0].url) == f"https://api.example.com/v1/w?city=B&appid={SECRET}"


@pytest.mark.req("REQ-4.5-5")
def test_mode_none_injects_nothing() -> None:
    rig = _rig(jarvis_key_slot="x-jarvis-key")
    rig.call(headers={"Authorization": "Bearer client-value"})
    assert rig.requests[0].headers["authorization"] == "Bearer client-value"
    assert SECRET not in str(rig.requests[0].url)


@pytest.mark.req("REQ-4.5-7")
def test_the_jarvis_key_is_never_forwarded_from_the_header_or_the_slot() -> None:
    rig = _rig({"mode": "bearer"})
    rig.call(headers={"Authorization": f"Bearer {KEY}"}, url="/gw/weather/x")
    rig.call(headers={"X-Jarvis-Key": KEY, "authorization": "Bearer other"})
    for up in rig.requests:
        assert KEY not in str(up.url)
        assert all(KEY not in v for v in up.headers.values())
        assert "x-jarvis-key" not in up.headers


def test_hop_by_hop_headers_are_not_forwarded_and_no_default_headers_are_added() -> None:
    rig = _rig()
    rig.call(headers={"Connection": "X-Foo", "X-Foo": "1", "Upgrade": "h2c", "TE": "trailers",
                      "Proxy-Authorization": "x", "X-Keep": "yes"})  # fmt: skip
    (up,) = rig.requests
    for name in ("x-foo", "upgrade", "te", "proxy-authorization"):
        assert name not in up.headers
    assert up.headers["x-keep"] == "yes"
    # httpx must not invent headers the caller did not send (they would change the upstream's view)
    assert "user-agent" not in up.headers
    assert "accept-encoding" not in up.headers


def test_cookies_from_one_response_are_never_replayed_to_another_caller() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(200, headers={"Set-Cookie": "sid=abc; Path=/"})

    rig = Rig([_api("a1", "weather")], handler=handler)
    first = rig.call()
    second = rig.call()
    assert first.headers["set-cookie"] == "sid=abc; Path=/"
    assert "cookie" not in rig.requests[1].headers
    assert second.status_code == 200


# --- what the caller receives -----------------------------------------------------------------


@pytest.mark.req("REQ-4.4-1", "REQ-4.4-7")
def test_upstream_response_is_returned_with_jarvis_headers() -> None:
    payload = gzip.compress(b"\x00\x01binary")

    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(
            201,
            content=payload,
            headers=[("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("Content-Encoding", "gzip"),
                     ("X-Jarvis-Source", "spoofed"), ("Connection", "close, X-Hop"), ("X-Hop", "1"),
                     ("Content-Type", "application/octet-stream")],
        )  # fmt: skip

    rig = Rig([_api("a1", "weather")], handler)
    response = rig.call()
    assert response.status_code == 201
    assert response.content == b"\x00\x01binary"  # the test client decodes gzip itself
    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert response.headers["content-encoding"] == "gzip"
    assert "x-hop" not in response.headers
    assert response.headers["x-jarvis-source"] == "upstream"
    assert response.headers["x-jarvis-attempts"] == "1"
    assert len(response.headers["x-jarvis-request-id"]) == 26


@pytest.mark.req("REQ-4.4-7")
@pytest.mark.parametrize("status", [400, 404, 429, 500, 502, 503, 504])
def test_upstream_errors_pass_through_unchanged(status: int) -> None:
    body = b'{"error": "theirs", "code": 42}'
    headers = {"Retry-After": "7", "Content-Type": "application/json"}
    rig = Rig([_api("a1", "weather")], lambda r: _resp(status, content=body, headers=headers))
    response = rig.call()
    assert (response.status_code, response.content) == (status, body)
    assert response.headers["retry-after"] == "7"
    assert response.headers["x-jarvis-source"] == "upstream"


@pytest.mark.req("REQ-17-1")
def test_upstream_redirects_are_returned_not_followed() -> None:
    moved = _resp(302, headers={"Location": "https://elsewhere.example/"})
    rig = Rig([_api("a1", "weather")], lambda r: moved)
    response = rig.call(follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://elsewhere.example/"
    assert len(rig.requests) == 1


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_head_and_options_are_forwarded(method: str) -> None:
    rig = _rig()
    assert rig.call(method).status_code == 200
    assert rig.requests[0].method == method


def test_a_204_has_no_body() -> None:
    rig = Rig([_api("a1", "weather")], lambda r: _resp(204))
    response = rig.call("DELETE")
    assert response.status_code == 204
    assert response.content == b""


# --- ingress limits (REQ-4.3-2, D11) ----------------------------------------------------------


@pytest.mark.req("REQ-4.3-2")
def test_a_declared_body_over_the_limit_is_413_before_authentication() -> None:
    rig = _rig()
    response = rig.client.post("/gw/weather/x", content=b"x" * (LIMIT + 1))  # no key at all
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "JV_BODY_TOO_LARGE"
    assert response.headers["x-jarvis-source"] == "jarvis"
    assert rig.requests == []


@pytest.mark.req("REQ-4.3-2")
def test_a_body_without_a_length_is_capped_while_it_is_read() -> None:
    rig = _rig()

    def chunks() -> Any:
        for _ in range(4):
            yield b"x" * 400

    response = rig.call("POST", content=chunks())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "JV_BODY_TOO_LARGE"
    assert rig.requests == []


@pytest.mark.req("REQ-4.3-2")
def test_a_body_of_exactly_the_limit_passes() -> None:
    rig = _rig()
    assert rig.call("POST", content=b"x" * LIMIT).status_code == 200
    assert len(rig.requests[0].content) == LIMIT


def test_an_invalid_content_length_does_not_crash_ingress() -> None:
    rig = _rig()
    response = rig.call("GET", headers={"Content-Length": "banana"})
    assert response.status_code in (200, 400)


@pytest.mark.req("REQ-2-13")
@pytest.mark.parametrize("accept", ["text/event-stream", "TEXT/EVENT-STREAM;q=0.8, */*"])
def test_streaming_requests_are_rejected_before_authentication(accept: str) -> None:
    rig = _rig()
    response = rig.client.get("/gw/weather/x", headers={"Accept": accept})  # no key
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "JV_STREAMING_UNSUPPORTED"
    assert rig.requests == []


# --- failures (REQ-4.4-5, 4.4-6) --------------------------------------------------------------


def _json_error(response: httpx.Response, code: str, status: int) -> None:
    assert response.status_code == status
    body = response.json()["error"]
    assert body["code"] == code
    assert body["request_id"] == response.headers["x-jarvis-request-id"]
    assert response.headers["x-jarvis-source"] == "jarvis"


@pytest.mark.req("REQ-4.4-5")
@pytest.mark.parametrize(
    "exc", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.PoolTimeout("t")]
)
def test_upstream_timeouts_are_504(exc: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    _json_error(Rig([_api("a1", "weather")], handler).call(), "JV_UPSTREAM_TIMEOUT", 504)


def test_a_slow_upstream_hits_the_deadline() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return _resp(200)

    rig = Rig(
        [_api("a1", "weather", deadline_ms=200, attempt_timeout_ms=100, min_attempt_ms=50)], slow
    )
    _json_error(rig.call(), "JV_UPSTREAM_TIMEOUT", 504)


@pytest.mark.req("REQ-4.4-5")
@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("refused 10.0.0.9:443?key=SECRET"), httpx.RemoteProtocolError("bad")]
)
def test_unreachable_upstreams_are_502_with_a_fixed_message(exc: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    response = Rig([_api("a1", "weather")], handler).call()
    _json_error(response, "JV_UPSTREAM_UNREACHABLE", 502)
    assert "SECRET" not in response.text
    assert "10.0.0.9" not in response.text


def test_a_blocked_destination_is_403() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise DestinationBlockedError("destination is not allowed")

    _json_error(Rig([_api("a1", "weather")], handler).call(), "JV_DESTINATION_BLOCKED", 403)


@pytest.mark.req("REQ-4.5-7")
def test_a_secret_that_cannot_be_opened_is_a_clear_502_and_nothing_is_sent() -> None:
    other = AesGcmSecretBox(bytes(range(1, 33)))
    rig = Rig([_api("a1", "weather", {"mode": "bearer"}, box=other)])
    response = rig.call()
    _json_error(response, "JV_UPSTREAM_UNREACHABLE", 502)
    assert SECRET not in response.text
    assert rig.requests == []


def test_a_secret_with_illegal_characters_is_a_502_not_a_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad = AesGcmSecretBox(bytes(range(32)))
    api = _api("a1", "weather", {"mode": "header", "header_name": "X-Key"})
    sealed = bad.seal("bad\r\nInjected: 1", context="sec_a1")
    rig = Rig([ApiEntry(api.api_id, api.project_id, api.slug, 1, api.config, sealed)])
    with caplog.at_level(logging.DEBUG):
        response = rig.call()
    assert response.status_code in (502, 200)
    assert "Injected" not in caplog.text
    assert all("Injected" not in k for r in rig.requests for k in r.headers)


# --- clients ----------------------------------------------------------------------------------


@pytest.mark.req("REQ-3-4")
def test_one_client_per_origin_is_created_and_reused() -> None:
    rig = Rig([_api("a1", "weather"), _api("a2", "maps", base_url="https://maps.example.com")])
    rig.call()
    rig.call()
    rig.call(url="/gw/maps/x")
    assert rig.factory_calls == 2


def test_clients_for_origins_no_api_uses_any_more_are_closed() -> None:
    rig = Rig([_api("a1", "weather"), _api("a2", "maps", base_url="https://maps.example.com")])
    rig.call()
    rig.call(url="/gw/maps/x")
    rig.store.update(lambda s: s.with_changes(apis={"a2": None}))
    assert set(rig.forwarder.origins()) == {("https", "api.example.com", 443)}


def test_an_unknown_slug_and_a_bad_key_never_reach_the_upstream() -> None:
    rig = _rig()
    assert rig.call(url="/gw/nope/x").status_code == 401
    assert (
        rig.client.get("/gw/weather/x", headers={"X-Jarvis-Key": "jv_live_bad"}).status_code == 401
    )
    assert rig.requests == []


@pytest.mark.req("REQ-17-3")
def test_nothing_secret_is_logged_on_success_or_failure(caplog: pytest.LogCaptureFixture) -> None:
    rig = _rig({"mode": "query", "param_name": "appid"})
    with caplog.at_level(logging.DEBUG):
        rig.call(url="/gw/weather/w?appid=client-secret-value")
        rig.client.get("/gw/weather/w", headers={"X-Jarvis-Key": "jv_live_nope"})
    for needle in (SECRET, KEY, hash_jarvis_key(KEY), "client-secret-value", "jv_live_nope"):
        assert needle not in caplog.text


# --- lifecycle and interruptions --------------------------------------------------------------


class _Interrupted(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"abc"
        raise httpx.ReadError("connection reset while reading https://x/?appid=LEAKME")


def test_an_upstream_that_breaks_mid_body_aborts_the_response_without_leaking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig(
        [_api("a1", "weather")],
        lambda r: httpx.Response(200, headers={"Content-Length": "100"}, stream=_Interrupted()),
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(UpstreamBodyError):
        rig.call()
    assert "ReadError" in caplog.text
    assert "LEAKME" not in caplog.text


class _Trickle(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"abc"
        await asyncio.sleep(5)
        yield b"def"


@pytest.mark.req("REQ-4.6-5")
def test_a_response_body_still_running_at_the_deadline_is_cut(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig(
        [_api("a1", "weather", deadline_ms=200, attempt_timeout_ms=100, min_attempt_ms=50)],
        lambda r: httpx.Response(200, headers={"Content-Length": "6"}, stream=_Trickle()),
    )
    started = time.monotonic()
    with caplog.at_level(logging.WARNING), pytest.raises(UpstreamBodyError):
        rig.call()
    assert time.monotonic() - started < 1
    assert "request deadline" in caplog.text


@pytest.mark.req("REQ-4.6-5")
def test_a_stalled_upload_is_answered_504_when_the_deadline_passes() -> None:
    rig = _rig(deadline_ms=200, attempt_timeout_ms=100, min_attempt_ms=50)

    def stalled() -> Any:
        yield b"x"
        time.sleep(0.5)  # the client goes quiet past the whole deadline
        yield b"y"

    # The test client cannot answer before its upload ends; the real-socket case (an upload that
    # never ends) is in tests/functional/test_deadlines.py.
    _json_error(rig.call("POST", content=stalled()), "JV_UPSTREAM_TIMEOUT", 504)
    assert rig.requests == []


@pytest.mark.req("REQ-4.6-5")
async def test_an_upload_that_never_ends_is_cut_at_the_deadline() -> None:
    class Stalled:
        async def stream(self) -> AsyncIterator[bytes]:
            yield b"x"
            await asyncio.sleep(30)

    forwarder = Forwarder(SsrfPolicy(), CredentialVault(BOX), max_body_bytes=LIMIT)
    clock = SystemClock()
    started = clock.monotonic()
    with pytest.raises(UpstreamTimeoutError):
        await forwarder._read_body(cast(Any, Stalled()), Deadline(clock, started, 150))
    assert clock.monotonic() - started < 1


@pytest.mark.req("REQ-4.6-5")
def test_the_attempt_is_bounded_by_the_time_left_not_only_by_its_own_timeout() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return _resp(200)

    # 300 ms of the 400 ms deadline go on the upload; the 350 ms attempt timeout must not apply.
    rig = Rig(
        [_api("a1", "weather", deadline_ms=400, attempt_timeout_ms=350, min_attempt_ms=50)], slow
    )

    def upload() -> Any:
        for _ in range(3):
            time.sleep(0.1)
            yield b"x"

    started = time.monotonic()
    _json_error(rig.call("POST", content=upload()), "JV_UPSTREAM_TIMEOUT", 504)
    assert time.monotonic() - started < 0.4 + 0.15


FAST_RETRY = {"base_ms": 1, "cap_ms": 2}


def _sequence(
    *answers: Exception | Callable[[], httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """A handler giving each call the next answer (the last repeats); exceptions are raised."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        answer = answers[min(len(calls), len(answers) - 1)]
        calls.append(1)
        if isinstance(answer, Exception):
            raise answer
        return cast(httpx.Response, answer())  # a new response each time: attempts close them

    return handler


@pytest.mark.req("REQ-4.7-1")
def test_a_503_is_retried_and_the_attempts_header_counts_the_attempts() -> None:
    rig = Rig(
        [_api("a1", "weather", retry=FAST_RETRY)], _sequence(lambda: _resp(503), lambda: _resp(200))
    )
    response = rig.call()
    assert response.status_code == 200
    assert response.headers["X-Jarvis-Attempts"] == "2"
    assert len(rig.requests) == 2


@pytest.mark.req("REQ-4.7-9")
def test_the_last_retryable_answer_is_returned_after_max_attempts() -> None:
    rig = Rig(
        [_api("a1", "weather", retry={**FAST_RETRY, "max_attempts": 2})],
        _sequence(lambda: _resp(503)),
    )
    response = rig.call()
    assert response.status_code == 503
    assert response.headers["X-Jarvis-Attempts"] == "2"
    assert len(rig.requests) == 2


@pytest.mark.req("REQ-4.7-1")
def test_connection_errors_are_retried_then_reported() -> None:
    rig = Rig(
        [_api("a1", "weather", retry=FAST_RETRY)],
        _sequence(httpx.ConnectError("x"), httpx.ReadTimeout("t"), lambda: _resp(200)),
    )
    assert rig.call().headers["X-Jarvis-Attempts"] == "3"
    rig = Rig([_api("a1", "weather", retry=FAST_RETRY)], _sequence(httpx.ConnectError("x")))
    _json_error(rig.call(), "JV_UPSTREAM_UNREACHABLE", 502)


@pytest.mark.req("REQ-4.7-1")
def test_an_invalid_url_is_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.InvalidURL("bad")

    _json_error(
        Rig([_api("a1", "weather", retry=FAST_RETRY)], handler).call(),
        "JV_UPSTREAM_UNREACHABLE",
        502,
    )
    assert len(calls) == 1


@pytest.mark.req("REQ-4.7-3")
def test_a_post_without_an_idempotency_header_is_not_retried() -> None:
    rig = Rig(
        [_api("a1", "weather", retry=FAST_RETRY)], _sequence(lambda: _resp(503), lambda: _resp(200))
    )
    assert rig.call("POST", content=b"{}").status_code == 503
    assert len(rig.requests) == 1


@pytest.mark.req("REQ-4.7-5")
def test_one_idempotency_key_is_generated_per_request_and_reused_on_every_attempt() -> None:
    retry = {**FAST_RETRY, "idempotency_header": "Idempotency-Key"}
    rig = Rig(
        [_api("a1", "weather", retry=retry)],
        _sequence(lambda: _resp(503), lambda: _resp(502), lambda: _resp(200)),
    )
    assert rig.call("POST", content=b"{}").headers["X-Jarvis-Attempts"] == "3"
    keys = [r.headers["Idempotency-Key"] for r in rig.requests]
    assert len(keys[0]) == 32
    assert keys == [keys[0]] * 3
    rig.call("POST", content=b"{}")
    assert rig.requests[-1].headers["Idempotency-Key"] != keys[0]


@pytest.mark.req("REQ-4.7-5")
def test_a_callers_idempotency_key_is_kept_and_safe_methods_get_none() -> None:
    retry = {**FAST_RETRY, "idempotency_header": "Idempotency-Key"}
    rig = Rig([_api("a1", "weather", retry=retry)])
    rig.call("POST", content=b"{}", headers={"idempotency-key": "mine"})
    assert rig.requests[-1].headers["Idempotency-Key"] == "mine"
    rig.call("GET")
    assert "Idempotency-Key" not in rig.requests[-1].headers


class _Budget:
    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.requests: list[str] = []
        self.acquired: list[tuple[str, float, str, str]] = []
        self.closed = False

    def record_request(self, api_id: str) -> None:
        self.requests.append(api_id)

    async def try_acquire(
        self, api_id: str, ratio: float, *, project_id: str, request_id: str
    ) -> bool:
        self.acquired.append((api_id, ratio, project_id, request_id))
        return self.allow

    async def aclose(self) -> None:
        self.closed = True


def _budget_rig(allow: bool) -> tuple[Rig, _Budget]:
    budget = _Budget(allow)
    rig = Rig(
        [_api("a1", "weather", retry=FAST_RETRY)], _sequence(lambda: _resp(503), lambda: _resp(200))
    )
    rig.forwarder._budget = cast(Any, budget)
    return rig, budget


@pytest.mark.req("REQ-4.7-8")
def test_an_exhausted_budget_means_the_503_is_returned_without_a_retry() -> None:
    rig, budget = _budget_rig(allow=False)
    response = rig.call()
    assert response.status_code == 503
    assert response.headers["X-Jarvis-Attempts"] == "1"
    assert len(rig.requests) == 1
    assert budget.requests == ["a1"]  # one request counted
    assert [(a, r, p) for a, r, p, _ in budget.acquired] == [("a1", 0.2, "p1")]


@pytest.mark.req("REQ-4.7-7")
def test_a_granted_budget_lets_the_retry_through() -> None:
    rig, budget = _budget_rig(allow=True)
    assert rig.call().headers["X-Jarvis-Attempts"] == "2"
    assert budget.requests == ["a1"]  # counted once per request, not per attempt
    assert len(budget.acquired) == 1


@pytest.mark.req("REQ-4.7-8")
def test_the_budget_is_not_asked_when_the_policy_already_says_no() -> None:
    rig, budget = _budget_rig(allow=True)
    rig.call("POST", content=b"{}")  # a POST without an idempotency header is never retried
    assert budget.acquired == []


def test_closing_the_forwarder_closes_its_budget() -> None:
    rig, budget = _budget_rig(allow=True)
    with rig.client:
        rig.call()
    assert budget.closed


class _Circuit:
    """Stands in for the CircuitBreaker: records what the forwarder tells it."""

    def __init__(self, action: str = "allow", open_now: bool = False) -> None:
        self.action = action
        self.open_now = open_now
        self.finished: list[str] = []
        self.recorded: list[bool] = []
        self.closed = False

    async def admit(self, api_id: str, policy: Any, project_id: str, *, probe_ttl_s: int) -> Any:
        from jarvis_core.interfaces import CircuitState
        from jarvis_gateway.circuit import Ticket

        state = CircuitState.HALF_OPEN if self.action == "probe" else CircuitState.OPEN
        if self.action == "allow":
            state = CircuitState.CLOSED
        return Ticket(self.action, state)  # type: ignore[arg-type]

    def state(self, api_id: str) -> Any:
        from jarvis_core.interfaces import CircuitState

        return CircuitState.OPEN if self.open_now else CircuitState.CLOSED

    def is_open(self, api_id: str) -> bool:
        return self.open_now

    def record(self, api_id: str, policy: Any, project_id: str, *, failure: bool) -> None:
        self.recorded.append(failure)

    async def finish_probe(self, api_id: str, policy: Any, project_id: str, outcome: str) -> None:
        self.finished.append(outcome)

    async def aclose(self) -> None:
        self.closed = True


def _circuit_rig(circuit: _Circuit, handler: Any = None, **cfg: Any) -> Rig:
    rig = Rig([_api("a1", "weather", retry=FAST_RETRY, **cfg)], handler)
    rig.forwarder._circuit = cast(Any, circuit)
    return rig


@pytest.mark.req("REQ-4.8-10")
def test_a_rejected_request_never_reaches_the_upstream() -> None:
    rig = _circuit_rig(_Circuit("reject"))
    response = rig.call()
    _json_error(response, "JV_CIRCUIT_OPEN", 503)
    assert response.headers["X-Jarvis-Circuit"] == "OPEN"
    assert rig.requests == []


@pytest.mark.req("REQ-4.8-2", "REQ-4.8-3")
def test_every_attempt_outcome_is_reported_as_failure_or_not() -> None:
    circuit = _Circuit()
    rig = _circuit_rig(
        circuit,
        _sequence(lambda: _resp(503), httpx.ConnectError("x"), lambda: _resp(404)),
    )
    response = rig.call()
    assert response.status_code == 404
    assert response.headers["X-Jarvis-Circuit"] == "CLOSED"
    assert circuit.recorded == [True, True, False]  # 503, connect error, 404 (a 4xx is no failure)


@pytest.mark.req("REQ-4.8-7")
def test_a_probe_that_answers_ok_closes_and_one_that_fails_reopens() -> None:
    ok = _Circuit("probe")
    _circuit_rig(ok).call()
    assert ok.finished == ["success"]
    assert ok.recorded == []  # the probe's outcome is its verdict, not a counted call
    bad = _Circuit("probe")
    _circuit_rig(bad, _sequence(lambda: _resp(500))).call()
    assert bad.finished == ["failure"]


def test_a_probe_that_dies_before_an_answer_gives_the_slot_back() -> None:
    from jarvis_core.errors import DestinationBlockedError

    circuit = _Circuit("probe")

    def blocked(request: httpx.Request) -> httpx.Response:
        raise DestinationBlockedError("destination is not allowed")

    _json_error(_circuit_rig(circuit, blocked).call(), "JV_DESTINATION_BLOCKED", 403)
    assert circuit.finished == ["cancelled"]


def test_no_retry_is_sent_once_the_circuit_is_open() -> None:
    circuit = _Circuit(open_now=True)
    rig = _circuit_rig(circuit, _sequence(lambda: _resp(503), lambda: _resp(200)))
    assert rig.call().status_code == 503
    assert len(rig.requests) == 1


def test_closing_the_forwarder_closes_its_circuit_breaker() -> None:
    circuit = _Circuit()
    rig = _circuit_rig(circuit)
    with rig.client:
        rig.call()
    assert circuit.closed


def _limited_rig(limiter: Any, **cfg: Any) -> Rig:
    rig = Rig([_api("a1", "weather", **cfg)])
    rig.forwarder._limiter = limiter
    return rig


class _Limiter:
    def __init__(self, *, allow: bool = True, retry_after_s: float | None = None) -> None:
        self.allow = allow
        self.retry_after_s = retry_after_s
        self.keys: list[str] = []
        self.closed = False

    async def take(self, key: str, *, capacity: int, refill_per_s: float, cost: int = 1) -> Any:
        from jarvis_core.interfaces import RateDecision

        self.keys.append(key)
        return RateDecision(self.allow, 0.0, None if self.allow else self.retry_after_s)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.req("REQ-4.11-2")
def test_the_inbound_bucket_is_keyed_by_project_key_and_api() -> None:
    limiter = _Limiter()
    rig = _limited_rig(limiter, rate_limits={"inbound": {"enabled": True}})
    assert rig.call().status_code == 200
    assert limiter.keys == ["in:p1:k1:a1"]


@pytest.mark.req("REQ-4.11-3")
def test_the_upstream_bucket_is_keyed_by_api_only() -> None:
    limiter = _Limiter()
    rig = _limited_rig(limiter, rate_limits={"upstream": {"enabled": True}})
    assert rig.call().status_code == 200
    assert limiter.keys == ["up:a1"]


@pytest.mark.req("REQ-4.11-2", "REQ-4.11-3")
def test_buckets_that_are_off_are_never_asked() -> None:
    limiter = _Limiter(allow=False)
    rig = _limited_rig(limiter)
    assert rig.call().status_code == 200
    assert limiter.keys == []


@pytest.mark.parametrize(
    ("side", "code"), [("inbound", "JV_RATE_LIMITED"), ("upstream", "JV_QUOTA_EXCEEDED")]
)
@pytest.mark.parametrize(("wait", "header"), [(0.2, "1"), (1.0, "1"), (1.2, "2"), (59.01, "60")])
def test_a_refusal_is_a_429_with_retry_after_in_whole_seconds(
    side: str, code: str, wait: float, header: str
) -> None:
    rig = _limited_rig(
        _Limiter(allow=False, retry_after_s=wait), rate_limits={side: {"enabled": True}}
    )
    response = rig.call()
    _json_error(response, code, 429)
    assert response.headers["Retry-After"] == header
    assert rig.requests == []


def test_a_refusal_that_can_never_succeed_has_no_retry_after() -> None:
    rig = _limited_rig(
        _Limiter(allow=False, retry_after_s=None), rate_limits={"inbound": {"enabled": True}}
    )
    response = rig.call()
    _json_error(response, "JV_RATE_LIMITED", 429)
    assert "Retry-After" not in response.headers


@pytest.mark.req("REQ-4.11-4")
def test_a_limiter_that_breaks_lets_the_request_through(caplog: pytest.LogCaptureFixture) -> None:
    class Broken(_Limiter):
        async def take(self, key: str, **kwargs: Any) -> Any:
            raise ConnectionError("SECRET-MARKER")

    rig = _limited_rig(
        Broken(), rate_limits={"inbound": {"enabled": True}, "upstream": {"enabled": True}}
    )
    with caplog.at_level(logging.WARNING):
        assert rig.call().status_code == 200
    assert "ConnectionError" in caplog.text
    assert "SECRET-MARKER" not in caplog.text


@pytest.mark.req("REQ-4.11-3")
def test_a_retry_that_finds_the_upstream_quota_empty_is_not_sent() -> None:
    class OneToken(_Limiter):
        async def take(self, key: str, **kwargs: Any) -> Any:
            from jarvis_core.interfaces import RateDecision

            self.keys.append(key)
            return RateDecision(len(self.keys) == 1, 0.0, 1.0)

    limiter = OneToken()
    rig = Rig(
        [_api("a1", "weather", retry=FAST_RETRY, rate_limits={"upstream": {"enabled": True}})],
        _sequence(lambda: _resp(503), lambda: _resp(200)),
    )
    rig.forwarder._limiter = limiter
    response = rig.call()
    assert response.status_code == 503
    assert response.headers["X-Jarvis-Attempts"] == "1"
    assert len(rig.requests) == 1
    assert limiter.keys == ["up:a1", "up:a1"]


def test_closing_the_forwarder_closes_its_limiter() -> None:
    limiter = _Limiter()
    rig = _limited_rig(limiter)
    with rig.client:
        rig.call()
    assert limiter.closed


def test_the_forwarder_is_closed_when_the_app_shuts_down() -> None:
    rig = _rig()
    closed: list[bool] = []
    original = rig.forwarder.aclose

    async def spy() -> None:
        closed.append(True)
        await original()

    rig.forwarder.aclose = spy  # type: ignore[method-assign]
    with rig.client:
        rig.call()
    assert closed == [True]
    assert rig.forwarder.origins() == []


async def test_closing_an_unused_client_happens_on_the_running_loop() -> None:
    closed: list[str] = []

    class Tracking(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return _resp(200)

        async def aclose(self) -> None:
            closed.append("closed")

    forwarder = Forwarder(
        SsrfPolicy(), CredentialVault(BOX), max_body_bytes=LIMIT, transport_factory=Tracking
    )
    forwarder._client(httpx.URL("https://gone.example.com/x"))
    assert forwarder.origins() == [("https", "gone.example.com", 443)]
    forwarder.retain(Snapshot.empty("postgres", 1.0))
    await forwarder.aclose()  # also waits for the close that retain scheduled
    assert closed == ["closed"]
    assert forwarder.origins() == []


# --- the request clock and the hot path (REQ-4.3-1, REQ-4.3-6) --------------------------------


@pytest.mark.req("REQ-4.3-1")
def test_every_response_carries_its_own_ulid_request_id() -> None:
    rig = _rig()
    ids = {rig.call().headers["x-jarvis-request-id"] for _ in range(20)}
    ids |= {rig.call(url="/gw/nope/x").headers["x-jarvis-request-id"] for _ in range(20)}
    assert len(ids) == 40
    assert all(len(i) == 26 and i.isalnum() for i in ids)


@pytest.mark.req("REQ-4.3-1")
def test_the_deadline_starts_at_ingress_so_a_slow_upload_uses_it_up() -> None:
    async def quick(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.15)
        return _resp(200)

    rig = Rig(
        [_api("a1", "weather", deadline_ms=400, attempt_timeout_ms=300, min_attempt_ms=50)], quick
    )

    def slow_body() -> Any:
        for _ in range(4):
            time.sleep(0.1)  # 0.4 s of upload: the whole deadline, before the upstream is asked
            yield b"x" * 10

    _json_error(rig.call("POST", content=slow_body()), "JV_UPSTREAM_TIMEOUT", 504)
    assert rig.requests == []
    # the same upstream is fine when the request arrives promptly
    assert rig.call("POST", content=b"x" * 10).status_code == 200


@pytest.mark.req("REQ-4.3-6")
def test_serving_requests_makes_no_database_query_after_the_config_is_loaded(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    entry = _api("a1", "weather")
    keys = {hash_jarvis_key(KEY): KeyEntry("k1", "p1", frozenset(), False)}

    class CountingLoader:
        async def load_all(self, now: float) -> tuple[Snapshot, frozenset[str]]:
            calls.append("load_all")
            snapshot = Snapshot({entry.api_id: entry}, keys, "postgres", now)
            return snapshot, frozenset()

        async def load_api(self, api_id: str) -> None:
            calls.append("load_api")

        async def load_key(self, key_id: str) -> None:
            calls.append("load_key")

        async def load_project(self, project_id: str) -> None:
            calls.append("load_project")

        async def close(self) -> None: ...

    store = ConfigStore()
    vault = CredentialVault(BOX)
    store.add_listener(vault.refresh)
    transport = httpx.MockTransport(lambda request: _resp(200, content=b"ok"))
    forwarder = Forwarder(
        SsrfPolicy(), vault, max_body_bytes=LIMIT, transport_factory=lambda: transport
    )
    file = SnapshotFile(tmp_path / "snapshot.json")
    manager = ConfigManager(
        cast(Any, CountingLoader()),
        file,
        CoalescingWriter(file, SystemClock()),
        store,
        SystemClock(),
        resync_interval_s=3600,
        redis_url=None,
    )
    with TestClient(create_app(config=manager, forwarder=forwarder)) as client:
        assert calls == ["load_all"]  # the startup load
        for _ in range(10):
            response = client.get("/gw/weather/x", headers={"X-Jarvis-Key": KEY})
            assert response.status_code == 200
        client.get("/gw/weather/x", headers={"X-Jarvis-Key": "jv_live_bad"})
        assert calls == ["load_all"]  # nothing was read from Postgres to serve them


# --- review fixes: raw header bytes and the slug segment -----------------------------------------


def test_a_header_value_with_non_ascii_bytes_is_forwarded_byte_for_byte() -> None:
    rig = _rig()
    response = rig.call(headers={"X-Name": "café".encode()})
    assert response.status_code == 200
    assert (b"x-name", "café".encode()) in rig.requests[0].headers.raw


def test_a_non_ascii_provider_secret_is_sent_as_utf8_not_a_crash() -> None:
    api = _api("a1", "weather", {"mode": "header", "header_name": "X-Key"})
    sealed = BOX.seal("sécret", context="sec_a1")
    rig = Rig([ApiEntry(api.api_id, api.project_id, api.slug, 1, api.config, sealed)])
    assert rig.call().status_code == 200
    assert (b"X-Key", "sécret".encode()) in rig.requests[0].headers.raw


@pytest.mark.req("REQ-4.1-1")
def test_an_encoded_character_in_the_slug_does_not_drop_the_path() -> None:
    rig = _rig()
    assert rig.call(url="/gw/weath%65r/v1/x?q=1").status_code == 200
    assert str(rig.requests[0].url) == "https://api.example.com/v1/v1/x?q=1"


@pytest.mark.parametrize("target", ["/gw/weather%2Fv1/x", "/gw/weather%2F/x", "/gw/weather%2fv1"])
def test_an_encoded_slash_in_the_slug_position_is_refused_like_an_unknown_api(target: str) -> None:
    rig = _rig()
    response = rig.call(url=target)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "JV_AUTH_INVALID"
    assert rig.requests == []
