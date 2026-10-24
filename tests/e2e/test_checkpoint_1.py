"""Checkpoint 1 acceptance stories (IMPLEMENTATION, Phase 1: Proxy).

1. Given a project, a key and an API pointing at the mock upstream, when a client changes only its
   base URL and key, then requests succeed and the upstream receives the real credential and never
   the JARVIS key.
2. Given an API with base URL `http://169.254.169.254`, when I save it, then it is rejected.
3. Given a key revoked via the API, when the next request arrives 1 s later, then it is rejected.
4. Given Postgres is down and a snapshot exists, when the gateway restarts, then it serves traffic.
"""

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
import pytest
import requests

from tests.conftest import ADMIN_TOKEN, API_URL, PORTS, MockUpstream, compose, readyz, wait_until

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("compose_stack")]

GATEWAY = f"http://127.0.0.1:{PORTS['gateway']}"
PROVIDER_SECRET = "sk-checkpoint-1-provider-secret"


class ProviderClient:
    """A stand-in for a provider SDK: it lets you change the base URL and the API key, no more."""

    def __init__(self, api_key: str, base_url: str = "https://api.provider.example") -> None:
        self._session = requests.Session()
        self._base_url = base_url
        self._api_key = api_key

    def get(self, path: str) -> requests.Response:
        return self._session.get(
            f"{self._base_url}{path}",
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=10,
        )


@contextmanager
def project_with_api(slug: str) -> Iterator[tuple[httpx.Client, str, str, str, str]]:
    """(api client, project id, api id, key id, JARVIS key) for a mock-upstream API."""
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    with httpx.Client(base_url=f"{API_URL}/api/v1", headers=admin, timeout=10) as api:
        project = api.post("/projects", json={"name": slug, "slug": slug})
        assert project.status_code == 201, project.text
        pid = project.json()["id"]
        try:
            created = api.post(
                f"/projects/{pid}/apis",
                json={
                    "slug": "provider",
                    "name": "Provider",
                    "base_url": "http://mock-upstream:8080",
                    "jarvis_key_slot": "authorization",
                    "credential": {"mode": "bearer", "secret": PROVIDER_SECRET},
                },
            )
            assert created.status_code == 201, created.text
            key = api.post(f"/projects/{pid}/keys", json={"name": "app"})
            assert key.status_code == 201, key.text
            yield api, pid, created.json()["id"], key.json()["id"], key.json()["key"]
        finally:
            # Postgres may have only just returned: retry until the project is really gone.
            wait_until(
                lambda: api.delete(f"/projects/{pid}").status_code in (204, 404) or None,
                timeout_s=60,
                what="the project to be deleted",
            )


def _serving(client: ProviderClient, path: str = "/v1/ping") -> requests.Response | None:
    response = client.get(path)
    return response if response.status_code == 200 else None


@pytest.mark.req(
    "REQ-3-4",
    "REQ-4.1-1",
    "REQ-4.5-2",
    "REQ-4.5-6",
    "REQ-4.5-7",
    "REQ-1.1-2",
    "REQ-3-1",
    "REQ-2-12",
)
def test_story_1_change_the_base_url_and_key_and_the_upstream_sees_the_real_credential(
    mock_upstream: MockUpstream,
) -> None:
    with project_with_api("cp1-story-1") as (_, _, _, _, jarvis_key):
        client = ProviderClient(jarvis_key, base_url=f"{GATEWAY}/gw/provider")
        response = wait_until(
            lambda: _serving(client), timeout_s=10, interval_s=0.1, what="the first proxied call"
        )
        assert response.headers["x-jarvis-source"] == "upstream"
        (call,) = mock_upstream.calls("/v1/ping")
        assert call["headers"]["authorization"] == f"Bearer {PROVIDER_SECRET}"  # type: ignore[index]
        assert jarvis_key not in json.dumps(call)
        assert "jv_" not in json.dumps(call)


@pytest.mark.req("REQ-4.13-1", "REQ-4.13-3")
def test_story_2_a_metadata_service_base_url_is_rejected_on_save() -> None:
    with project_with_api("cp1-story-2") as (api, pid, _, _, _):
        for url in ("http://169.254.169.254", "https://169.254.169.254/latest/meta-data"):
            response = api.post(
                f"/projects/{pid}/apis", json={"slug": "imds", "name": "IMDS", "base_url": url}
            )
            assert response.status_code == 422, (url, response.text)
            assert response.json()["detail"][0]["loc"] == ["body", "base_url"]
        listed = api.get(f"/projects/{pid}/apis").json()
        assert [a["slug"] for a in listed] == ["provider"]  # nothing was stored


@pytest.mark.req("REQ-11.1-6")
def test_story_3_a_revoked_key_is_rejected_one_second_later(mock_upstream: MockUpstream) -> None:
    with project_with_api("cp1-story-3") as (api, _, _, key_id, jarvis_key):
        client = ProviderClient(jarvis_key, base_url=f"{GATEWAY}/gw/provider")
        wait_until(lambda: _serving(client), timeout_s=10, interval_s=0.1, what="the first call")
        assert api.delete(f"/keys/{key_id}").status_code == 204
        time.sleep(1.0)
        rejected = client.get("/v1/ping")
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == "JV_AUTH_INVALID"


@pytest.mark.req("REQ-8-2", "REQ-8-9")
def test_story_4_with_postgres_down_a_restarted_gateway_serves_from_its_snapshot(
    mock_upstream: MockUpstream,
    kill: Callable[[str], None],
    revive: Callable[[str], None],
) -> None:
    with project_with_api("cp1-story-4") as (_, _, _, _, jarvis_key):
        client = ProviderClient(jarvis_key, base_url=f"{GATEWAY}/gw/provider")
        wait_until(lambda: _serving(client), timeout_s=10, interval_s=0.1, what="the first call")
        container = compose("ps", "-q", "gateway").stdout.strip()

        def snapshot_written() -> bool:
            return (
                compose(
                    "exec", "-T", "gateway", "test", "-s", "/var/lib/jarvis/gateway/snapshot.json",
                    check=False,
                ).returncode
                == 0
            )  # fmt: skip

        assert container
        wait_until(lambda: snapshot_written() or None, timeout_s=15, what="the snapshot file")
        kill("postgres")
        compose("restart", "gateway")
        wait_until(
            lambda: readyz("gateway").json()["config"]["source"] == "file" or None,
            timeout_s=60,
            what="the gateway to start from its snapshot",
        )
        seen: list[int] = []

        def poll() -> requests.Response | None:
            response = client.get("/v1/ping")
            seen.append(response.status_code)
            return response if response.status_code == 200 else None

        try:
            served = wait_until(
                poll, timeout_s=30, interval_s=0.2, what="traffic with Postgres down"
            )
        except AssertionError as error:
            raise AssertionError(f"{error}; statuses seen: {seen}") from None
        assert served.status_code == 200
        revive("postgres")
        wait_until(
            lambda: readyz("gateway").json()["config"]["source"] == "postgres" or None,
            timeout_s=90,
            what="the gateway to reload from Postgres",
        )
