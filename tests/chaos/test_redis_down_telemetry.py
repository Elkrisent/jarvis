"""Redis down: requests still succeed, telemetry is dropped and counted, then delivery resumes."""

from collections.abc import Callable

import httpx
import pytest

from tests.conftest import (
    ADMIN_TOKEN,
    API_URL,
    PORTS,
    MockUpstream,
    readyz,
    telemetry_events,
    wait_until,
)

pytestmark = [pytest.mark.chaos, pytest.mark.usefixtures("compose_stack")]

GATEWAY = f"http://127.0.0.1:{PORTS['gateway']}"


def _dropped() -> int:
    telemetry = readyz("gateway").json()["telemetry"]
    return int(telemetry["dropped_total"])


@pytest.mark.req("REQ-6.3-2", "REQ-6.3-4", "REQ-8-1", "REQ-3-2")
def test_requests_succeed_while_redis_is_down_and_events_resume_after(
    mock_upstream: MockUpstream,
    kill: Callable[[str], None],
    revive: Callable[[str], None],
) -> None:
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    with httpx.Client(base_url=f"{API_URL}/api/v1", headers=admin, timeout=10) as api:
        pid = api.post("/projects", json={"name": "Chaos", "slug": "telemetry-chaos"}).json()["id"]
        try:
            api.post(
                f"/projects/{pid}/apis",
                json={
                    "slug": "weather",
                    "name": "Weather",
                    "base_url": "http://mock-upstream:8080",
                    "jarvis_key_slot": "authorization",
                    "credential": {"mode": "none"},
                },
            ).raise_for_status()
            key = api.post(f"/projects/{pid}/keys", json={"name": "app"}).json()["key"]
            headers = {"X-Jarvis-Key": key}

            def call() -> httpx.Response:
                return httpx.get(f"{GATEWAY}/gw/weather/v1/x", headers=headers, timeout=10)

            wait_until(
                lambda: call().status_code == 200 or None,
                timeout_s=10,
                interval_s=0.1,
                what="the gateway to know the API",
            )
            before = _dropped()
            kill("redis")
            statuses = [call().status_code for _ in range(50)]
            assert statuses == [200] * 50  # the request path never noticed
            wait_until(
                lambda: _dropped() >= before + 50 or None,
                timeout_s=15,
                interval_s=0.3,
                what="the dropped counter to account for the 50 events",
            )
            assert readyz("gateway").status_code == 200  # degraded (D3), never unavailable

            revive("redis")
            wait_until(
                lambda: readyz("gateway").json()["status"] == "ok" or None,
                timeout_s=45,
                what="the gateway to report ok again",
            )
            response = wait_until(
                lambda: r if (r := call()).status_code == 200 else None,
                timeout_s=10,
                what="a call after Redis returned",
            )
            request_id = response.headers["x-jarvis-request-id"]
            wait_until(
                lambda: any(e["request_id"] == request_id for e in telemetry_events()) or None,
                timeout_s=30,
                interval_s=0.5,
                what="the first event after Redis returned",
            )
        finally:
            api.delete(f"/projects/{pid}")
