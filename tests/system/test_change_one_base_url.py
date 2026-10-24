"""The "change one base URL" story (M1.8 DoD), with a requests-based SDK-style client.

The client only lets you change two things, like most provider SDKs: the base URL and the API key.
Pointed at the gateway with a JARVIS key, it must work unchanged and the upstream must see the real
provider credential.
"""

import json
import time

import httpx
import pytest
import requests

from tests.conftest import ADMIN_TOKEN, API_URL, PORTS, MockUpstream, wait_until

pytestmark = [pytest.mark.system, pytest.mark.usefixtures("compose_stack")]

GATEWAY = f"http://127.0.0.1:{PORTS['gateway']}"
PROVIDER_SECRET = "sk-real-weather-provider-secret"
ROTATED_SECRET = "sk-rotated-weather-provider-secret"


class WeatherClient:
    """A stand-in for a provider SDK: configurable base URL and API key, nothing else."""

    def __init__(self, api_key: str, base_url: str = "https://api.weather.example") -> None:
        self._session = requests.Session()
        self._base_url = base_url
        self._api_key = api_key

    def current(self, city: str) -> requests.Response:
        return self._session.get(
            f"{self._base_url}/v1/current",
            params={"city": city},
            headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
            timeout=10,
        )


@pytest.mark.req(
    "REQ-3-4",
    "REQ-4.1-1",
    "REQ-4.5-2",
    "REQ-4.5-6",
    "REQ-4.5-7",
    "REQ-4.4-1",
    "REQ-1.1-2",
    "REQ-3-1",
    "REQ-2-12",
)
def test_a_client_works_unchanged_after_swapping_its_base_url_and_key(
    mock_upstream: MockUpstream,
) -> None:
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    with httpx.Client(base_url=f"{API_URL}/api/v1", headers=admin, timeout=10) as api:
        project = api.post("/projects", json={"name": "Story", "slug": "base-url-story"})
        assert project.status_code == 201, project.text
        pid = project.json()["id"]
        try:
            created = api.post(
                f"/projects/{pid}/apis",
                json={
                    "slug": "weather",
                    "name": "Weather",
                    "base_url": "http://mock-upstream:8080",
                    "jarvis_key_slot": "authorization",
                    "credential": {"mode": "bearer", "secret": PROVIDER_SECRET},
                },
            )
            assert created.status_code == 201, created.text
            key = api.post(f"/projects/{pid}/keys", json={"name": "app"})
            assert key.status_code == 201, key.text
            jarvis_key = key.json()["key"]
            client = WeatherClient(jarvis_key, base_url=f"{GATEWAY}/gw/weather")

            response = wait_until(
                lambda: r if (r := client.current("Bangalore")).status_code == 200 else None,
                timeout_s=10,
                interval_s=0.1,
                what="the gateway to forward the first call",
            )
            body = response.json()
            assert body["path"] == "/v1/current"
            assert body["query"] == [["city", "Bangalore"]]
            assert response.headers["x-jarvis-source"] == "upstream"
            assert len(response.headers["x-jarvis-request-id"]) == 26

            (call,) = mock_upstream.calls("/v1/current")
            assert call["headers"]["authorization"] == f"Bearer {PROVIDER_SECRET}"  # type: ignore[index]
            assert "jv_" not in json.dumps(call)
            assert call["method"] == "GET"

            # Rotating the provider secret through the management API is used by the next call.
            patched = api.patch(
                f"/apis/{created.json()['id']}",
                json={"credential": {"mode": "bearer", "secret": ROTATED_SECRET}},
            )
            assert patched.status_code == 200, patched.text
            started = time.monotonic()
            wait_until(
                lambda: (
                    r
                    if (r := client.current("Chennai")).json()["headers"]["authorization"]
                    == f"Bearer {ROTATED_SECRET}"
                    else None
                ),
                timeout_s=5,
                interval_s=0.05,
                what="the rotated provider secret to reach the upstream",
            )
            assert time.monotonic() - started <= 2.0

            # Revoking the JARVIS key stops the same client within a second.
            assert api.delete(f"/keys/{key.json()['id']}").status_code == 204
            wait_until(
                lambda: client.current("Mumbai").status_code == 401 or None,
                timeout_s=5,
                interval_s=0.05,
                what="the revoked key to be rejected",
            )
        finally:
            api.delete(f"/projects/{pid}")
