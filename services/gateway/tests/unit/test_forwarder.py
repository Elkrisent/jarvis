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
from jarvis_core.errors import DestinationBlockedError
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
