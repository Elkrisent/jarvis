"""M2.5: the inbound and upstream limiters on the real request path (SPEC §4.11)."""

from __future__ import annotations

import pytest
from jarvis_core.clock import SystemClock
from jarvis_core.memory import InMemoryRateLimiter
from rig import KEY, KEY2, GatewayFactory, Upstream
from test_retries import OK, _resp, scripted

pytestmark = pytest.mark.functional


def _limiter() -> InMemoryRateLimiter:
    return InMemoryRateLimiter(SystemClock())


def _bucket(capacity: int, refill: float = 0.01) -> dict[str, object]:
    return {"enabled": True, "capacity": capacity, "refill_per_s": refill}


@pytest.mark.req("REQ-4.11-2")
async def test_the_inbound_limiter_is_off_by_default(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    gateway = make_gateway(limiter=_limiter())
    for _ in range(30):
        assert (await gateway.call("GET", "/gw/none/x")).status_code == 200


@pytest.mark.req("REQ-4.11-2", "REQ-4.3-4")
async def test_inbound_buckets_are_separate_per_key(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    gateway = make_gateway(limiter=_limiter(), rate_limits={"inbound": _bucket(3)})
    for _ in range(3):
        assert (await gateway.call("GET", "/gw/none/x", key=KEY)).status_code == 200
    limited = await gateway.call("GET", "/gw/none/x", key=KEY)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "JV_RATE_LIMITED"
    assert limited.headers["X-Jarvis-Source"] == "jarvis"
    assert int(limited.headers["Retry-After"]) >= 1
    for _ in range(3):  # the other key has a bucket of its own
        assert (await gateway.call("GET", "/gw/none/x", key=KEY2)).status_code == 200
    assert (await gateway.call("GET", "/gw/none/x", key=KEY2)).status_code == 429


@pytest.mark.req("REQ-4.11-2")
async def test_inbound_buckets_are_separate_per_api(
    make_gateway: GatewayFactory, upstream: Upstream
) -> None:
    gateway = make_gateway(limiter=_limiter(), rate_limits={"inbound": _bucket(2)})
    for slug in ("bearer", "header"):
        assert (await gateway.call("GET", f"/gw/{slug}/x")).status_code == 200
        assert (await gateway.call("GET", f"/gw/{slug}/x")).status_code == 200
        assert (await gateway.call("GET", f"/gw/{slug}/x")).status_code == 429


@pytest.mark.req("REQ-4.11-6")
async def test_a_limited_request_never_reaches_the_upstream(
    make_gateway: GatewayFactory,
) -> None:
    async with scripted(OK) as upstream:
        gateway = make_gateway(
            port=upstream.port, limiter=_limiter(), rate_limits={"inbound": _bucket(2)}
        )
        for _ in range(6):
            await gateway.call("GET", "/gw/none/x")
        assert len(upstream.heads) == 2


@pytest.mark.req("REQ-4.11-3", "REQ-4.3-7")
async def test_the_upstream_quota_applies_across_keys_and_answers_quota_exceeded(
    make_gateway: GatewayFactory,
) -> None:
    async with scripted(OK) as upstream:
        gateway = make_gateway(
            port=upstream.port, limiter=_limiter(), rate_limits={"upstream": _bucket(4)}
        )
        statuses = []
        for n in range(8):
            response = await gateway.call("GET", "/gw/none/x", key=KEY if n % 2 else KEY2)
            statuses.append(response.status_code)
        assert statuses == [200] * 4 + [429] * 4
        assert response.json()["error"]["code"] == "JV_QUOTA_EXCEEDED"
        assert int(response.headers["Retry-After"]) >= 1
        assert len(upstream.heads) == 4  # only calls that leave the gateway are counted


@pytest.mark.req("REQ-4.11-3")
async def test_a_cached_or_rejected_request_does_not_spend_the_upstream_quota(
    make_gateway: GatewayFactory,
) -> None:
    async with scripted(OK) as upstream:
        gateway = make_gateway(
            port=upstream.port, limiter=_limiter(), rate_limits={"upstream": _bucket(2)}
        )
        for _ in range(5):  # bad keys are refused before step 7
            assert (
                await gateway.call("GET", "/gw/none/x", key="jv_live_nopenopenope")
            ).status_code == 401
        assert (await gateway.call("GET", "/gw/none/x")).status_code == 200
        assert (await gateway.call("GET", "/gw/none/x")).status_code == 200


@pytest.mark.req("REQ-4.11-3")
async def test_a_retry_needs_an_upstream_token_too(make_gateway: GatewayFactory) -> None:
    async with scripted(_resp(503), OK) as upstream:
        gateway = make_gateway(
            port=upstream.port,
            limiter=_limiter(),
            rate_limits={"upstream": _bucket(1)},
            retry={"base_ms": 1, "cap_ms": 2},
        )
        response = await gateway.call("GET", "/gw/none/x")
        assert response.status_code == 503  # the retry found the quota empty
        assert response.headers["X-Jarvis-Attempts"] == "1"
        assert len(upstream.heads) == 1
