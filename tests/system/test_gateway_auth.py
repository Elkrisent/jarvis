"""JARVIS key authentication on the real gateway container (M1.5)."""

import time

import httpx
import pytest

from tests.conftest import ADMIN_TOKEN, API_URL, PORTS, wait_until

pytestmark = [pytest.mark.system, pytest.mark.usefixtures("compose_stack")]

GATEWAY_URL = f"http://127.0.0.1:{PORTS['gateway']}"
BUDGET_S = 1.0


def _status(gateway: httpx.Client, key: str | None) -> int:
    headers = {} if key is None else {"X-Jarvis-Key": key}
    return gateway.get("/gw/mock/anything", headers=headers).status_code


@pytest.mark.req("REQ-4.2-2", "REQ-4.2-4", "REQ-4.2-6")
def test_missing_bad_and_revoked_keys_get_401_and_a_live_key_gets_through() -> None:
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    with (
        httpx.Client(base_url=f"{API_URL}/api/v1", headers=admin, timeout=10) as api,
        httpx.Client(base_url=GATEWAY_URL, timeout=10) as gateway,
    ):
        project = api.post("/projects", json={"name": "Auth", "slug": "gw-auth-test"})
        assert project.status_code == 201, project.text
        pid = project.json()["id"]
        made = api.post(
            f"/projects/{pid}/apis",
            json={"slug": "mock", "name": "Mock", "base_url": "http://mock-upstream:8080"},
        )
        assert made.status_code == 201, made.text
        key = api.post(f"/projects/{pid}/keys", json={"name": "ci"})
        assert key.status_code == 201, key.text

        # Usable once the gateway has seen it; the request is then forwarded (200 from the mock).
        wait_until(
            lambda: _status(gateway, key.json()["key"]) == 200 or None,
            timeout_s=10,
            interval_s=0.05,
            what="gateway to accept the new key",
        )
        for headers in ({}, {"X-Jarvis-Key": "jv_live_notarealkey"}):
            response = gateway.get("/gw/mock/anything", headers=headers)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "JV_AUTH_INVALID"

        started = time.monotonic()
        assert api.delete(f"/keys/{key.json()['id']}").status_code == 204
        wait_until(
            lambda: _status(gateway, key.json()["key"]) == 401 or None,
            timeout_s=5,
            interval_s=0.02,
            what="gateway to reject the revoked key",
        )
        assert time.monotonic() - started <= BUDGET_S
        api.delete(f"/projects/{pid}")
