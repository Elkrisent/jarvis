"""What the upstream receives and what the caller gets back, across methods, modes and bodies."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from rig import (
    BASE_PATH,
    KEY,
    MODES,
    OTHER_BOX,
    SECRET,
    GatewayFactory,
    Upstream,
    api_entry,
    free_port,
)

pytestmark = pytest.mark.functional

NO_BODY = ["GET", "HEAD", "OPTIONS", "DELETE"]
WITH_BODY = ["POST", "PUT", "PATCH"]
BODIES = {
    "empty": b"",
    "json": b'{"city": "Bangalore", "n": [1, 2, 3]}',
    "binary": os.urandom(256 * 1024)[: 60 * 1024],  # under the 64 KiB test limit
}


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    for i in range(0, len(data), 7000):
        yield data[i : i + 7000]


def _cases() -> list[tuple[str, str, str]]:
    cases = [(m, "none", mode) for m in NO_BODY for mode in MODES]
    for method in WITH_BODY:
        for kind in ("empty", "json", "binary", "chunked"):
            cases += [(method, kind, mode) for mode in MODES]
    return cases


def _assert_credential(call: dict[str, Any], mode: str) -> None:
    headers, query = call["headers"], dict(call["query"])
    if mode == "bearer":
        assert headers["authorization"] == f"Bearer {SECRET}"
    elif mode == "header":
        assert headers["x-api-key"] == SECRET
        assert "authorization" not in headers
    elif mode == "query":
        assert query["appid"] == SECRET
        assert "authorization" not in headers
    else:
        assert "authorization" not in headers
        assert "x-api-key" not in headers
        assert "appid" not in query


@pytest.mark.req(
    "REQ-4.1-1", "REQ-4.5-2", "REQ-4.5-3", "REQ-4.5-4", "REQ-4.5-5", "REQ-3-4", "REQ-2-12"
)
@pytest.mark.parametrize(("method", "body_kind", "mode"), _cases())
async def test_the_upstream_receives_exactly_the_forwarded_request(
    make_gateway: GatewayFactory, upstream: Upstream, method: str, body_kind: str, mode: str
) -> None:
    gateway = make_gateway()
    data = BODIES.get(body_kind, BODIES["json"])
    kwargs: dict[str, Any] = {}
    if body_kind == "chunked":
        kwargs["content"] = _chunks(data)
    elif method in WITH_BODY and data:
        kwargs["content"] = data
        kwargs["headers"] = {"Content-Type": "application/octet-stream"}
    response = await gateway.call(method, f"/gw/{mode}/v1/items/7?city=Bangalore&units=m", **kwargs)
    assert response.status_code == 200, response.text
    assert response.headers["x-jarvis-source"] == "upstream"
    (call,) = await upstream.calls()
    assert call["method"] == method
    assert call["path"] == f"{BASE_PATH}/v1/items/7"
    assert [q for q in call["query"] if q[0] in ("city", "units")] == [
        ["city", "Bangalore"],
        ["units", "m"],
    ]
    expected = len(data) if body_kind != "none" and method in WITH_BODY else 0
    assert call["body_size"] == expected
    assert call["headers"]["host"] == f"upstream.test:{upstream.port}"
    _assert_credential(call, mode)
    # the JARVIS key never travels on: not as a header, not in the query string
    assert all(KEY not in v for v in call["headers"].values())
    assert all(KEY not in q[1] for q in call["query"])
    assert "x-jarvis-key" not in call["headers"]


@pytest.mark.req("REQ-4.5-6", "REQ-2-12")
@pytest.mark.parametrize("mode", ["bearer", "header", "query"])
async def test_client_values_in_the_injected_slot_never_reach_the_upstream(
    make_gateway: GatewayFactory, upstream: Upstream, mode: str
) -> None:
    gateway = make_gateway()
    headers = {
        "authorization": "Bearer client-bearer",
        "Authorization": "Basic client-basic",
        "X-API-KEY": "client-key",
        "x-api-key": "client-key-2",
    }
    await gateway.call(
        "GET", f"/gw/{mode}/x?appid=client-q1&APPID=keep&appid=client-q2&city=B", headers=headers
    )
    (call,) = await upstream.calls()
    flat = str(call["headers"]) + str(call["query"])
    if mode == "bearer":
        assert call["headers"]["authorization"] == f"Bearer {SECRET}"
        assert "client-bearer" not in flat
        assert "client-basic" not in flat
        assert call["headers"]["x-api-key"] == "client-key, client-key-2"  # not our slot: kept
    elif mode == "header":
        assert call["headers"]["x-api-key"] == SECRET
        assert "client-key" not in flat
        # `authorization` is the JARVIS-key slot of these APIs: it is stripped, never forwarded
        assert "authorization" not in call["headers"]
    else:
        assert [v for k, v in call["query"] if k == "appid"] == [SECRET]
        assert "client-q1" not in flat
        assert "client-q2" not in flat
        assert ["APPID", "keep"] in call["query"]  # a different parameter name is untouched


@pytest.mark.req("REQ-4.4-7")
@pytest.mark.parametrize(
    "rule",
    [
        {"status": 429, "retry_after_s": 7},
        {"errors": [{"status": 503, "rate": 1.0}], "retry_after_s": 1},
        {"errors": [{"status": 500, "rate": 1.0}]},
        {"status": 404},
    ],
)
async def test_upstream_errors_pass_through_byte_for_byte(
    make_gateway: GatewayFactory, upstream: Upstream, rule: dict[str, Any]
) -> None:
    gateway = make_gateway()
    await upstream.rules({"path_prefix": "/", **rule})
    direct = await upstream.admin.get(f"{BASE_PATH}/v1/x")
    via = await gateway.call("GET", "/gw/none/v1/x")
    assert via.status_code == direct.status_code
    if "errors" in rule:
        assert via.content == direct.content
    assert via.headers.get("retry-after") == direct.headers.get("retry-after")
    assert via.headers["x-jarvis-source"] == "upstream"


@pytest.mark.req("REQ-4.4-7")
async def test_a_large_streamed_response_arrives_whole_with_its_length(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    gateway = make_gateway()
    await upstream.rules({"path_prefix": "/", "body_bytes": 2 * 1024 * 1024})
    response = await gateway.call("GET", "/gw/none/big")
    assert response.status_code == 200
    assert len(response.content) == 2 * 1024 * 1024
    assert response.headers["content-length"] == str(2 * 1024 * 1024)


@pytest.mark.req("REQ-17-1")
async def test_upstream_redirects_are_returned_not_followed(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    gateway = make_gateway()
    await upstream.rules({"path_prefix": "/", "redirect_to": "http://169.254.169.254/latest"})
    response = await gateway.call("GET", "/gw/none/x", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "http://169.254.169.254/latest"
    assert len(await upstream.calls()) == 1


# --- ingress and failures -----------------------------------------------------------------------


@pytest.mark.req("REQ-4.3-2")
async def test_oversized_bodies_are_413_with_and_without_a_declared_length(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    gateway = make_gateway(max_body_bytes=10_000)
    declared = await gateway.call("POST", "/gw/none/x", content=b"x" * 10_001)
    undeclared = await gateway.call("POST", "/gw/none/x", content=_chunks(b"x" * 10_001))
    for response in (declared, undeclared):
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "JV_BODY_TOO_LARGE"
        assert response.headers["x-jarvis-source"] == "jarvis"
    assert await upstream.calls() == []
    exact = await gateway.call("POST", "/gw/none/x", content=b"x" * 10_000)
    assert exact.status_code == 200


@pytest.mark.req("REQ-2-13")
async def test_streaming_requests_are_rejected_with_a_clear_error(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    gateway = make_gateway()
    response = await gateway.call(
        "GET", "/gw/none/stream", headers={"Accept": "text/event-stream"}, key=None
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "JV_STREAMING_UNSUPPORTED"
    assert await upstream.calls() == []


async def test_a_hung_upstream_is_a_504_at_the_deadline(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    gateway = make_gateway(deadline_ms=500, attempt_timeout_ms=400, min_attempt_ms=100)
    await upstream.rules({"path_prefix": "/", "hang": True})
    started = time.monotonic()
    response = await gateway.call("GET", "/gw/none/x")
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "JV_UPSTREAM_TIMEOUT"
    assert time.monotonic() - started < 3


async def test_a_refused_connection_is_a_502(make_gateway: GatewayFactory) -> None:
    gateway = make_gateway(port=free_port())  # nothing listens there
    response = await gateway.call("GET", "/gw/none/x")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "JV_UPSTREAM_UNREACHABLE"
    assert "127.0.0.1" not in response.text


@pytest.mark.req("REQ-4.1-2")
async def test_unknown_slugs_and_bad_keys_never_reach_the_upstream(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    gateway = make_gateway()
    assert (await gateway.call("GET", "/gw/nope/x")).status_code == 401
    assert (await gateway.call("GET", "/gw/none/x", key="jv_live_wrongwrong")).status_code == 401
    assert (await gateway.call("GET", "/gw/none/x", key=None)).status_code == 401
    assert await upstream.calls() == []


@pytest.mark.req("REQ-4.5-7")
async def test_a_secret_sealed_under_another_master_key_is_a_clear_502(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    gateway = make_gateway(box=OTHER_BOX)  # sealed with a key the gateway's vault does not have
    response = await gateway.call("GET", "/gw/bearer/x")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "JV_UPSTREAM_UNREACHABLE"
    assert SECRET not in response.text
    assert await upstream.calls() == []
    assert (await gateway.call("GET", "/gw/none/x")).status_code == 200  # others keep working


async def test_a_new_snapshot_with_a_replaced_secret_is_used_by_the_next_request(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    from rig import BOX

    gateway = make_gateway()
    await gateway.call("GET", "/gw/bearer/x")
    entry = api_entry("bearer-api", "bearer", MODES["bearer"], upstream.port)
    rotated = BOX.seal("sk-ROTATED", context="sec_bearer-api")
    new = type(entry)(entry.api_id, entry.project_id, entry.slug, 2, entry.config, rotated)
    gateway.store.update(lambda s: s.with_changes(apis={"bearer-api": new}))
    await gateway.call("GET", "/gw/bearer/x")
    first, second = await upstream.calls()
    assert first["headers"]["authorization"] == f"Bearer {SECRET}"
    assert second["headers"]["authorization"] == "Bearer sk-ROTATED"


# --- secrecy -------------------------------------------------------------------------------------


@pytest.mark.req("REQ-4.5-7", "REQ-17-3")
async def test_no_log_record_or_response_contains_a_secret_or_the_key(
    make_gateway: GatewayFactory, upstream: Upstream, caplog: pytest.LogCaptureFixture
) -> None:
    gateway = make_gateway()
    failures: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for mode in MODES:
            await gateway.call("GET", f"/gw/{mode}/x?appid=client-q")
            failures.append((await gateway.call("GET", f"/gw/{mode}/x", key="jv_live_nope")).text)
        await upstream.rules({"path_prefix": "/", "errors": [{"status": 500, "rate": 1.0}]})
        failures.append((await gateway.call("GET", "/gw/query/x")).text)
    # Logs and gateway-made or injected-error bodies: an upstream may echo what it was sent.
    rendered = caplog.text + "\n".join(failures)
    for needle in (SECRET, KEY, "jv_live_nope", "client-q"):
        assert needle not in rendered
