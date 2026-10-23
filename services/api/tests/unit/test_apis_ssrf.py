"""Save-time SSRF check on POST and PATCH of an API (M1.7, REQ-4.13-1)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from fakes import ADMIN_TOKEN, AUTH, FakeApis, FakeSecrets, fake_services
from fastapi.testclient import TestClient
from jarvis_api.app import create_app, create_app_from_env
from jarvis_api.http.deps import Services
from jarvis_core.clock import FakeClock
from jarvis_core.ssrf import DestinationChecker, DestinationResolver, SsrfPolicy

pytestmark = pytest.mark.unit

PUBLIC = "93.184.216.34"
SECRET = "SENTINEL-SECRET-1"
MESSAGE = "destination is not allowed"


def _checker(answers: dict[str, Sequence[str]], allowlist: str = "") -> DestinationChecker:
    async def resolve(host: str, port: int) -> Sequence[str]:
        return [] if port == 0 else answers.get(host, [])

    policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": allowlist})
    return DestinationChecker(DestinationResolver(policy, resolve, FakeClock()))


def _client(answers: dict[str, Sequence[str]], allowlist: str = "") -> tuple[TestClient, Services]:
    services = fake_services()
    services.destinations = _checker(answers, allowlist)
    return TestClient(create_app(services=services, admin_token=ADMIN_TOKEN)), services


def _project(client: TestClient) -> str:
    response = client.post("/api/v1/projects", json={"name": "P", "slug": "p"}, headers=AUTH)
    assert response.status_code == 201
    return str(response.json()["id"])


def _body(base_url: str, slug: str = "weather") -> dict[str, Any]:
    return {
        "slug": slug,
        "name": "Weather",
        "base_url": base_url,
        "credential": {"mode": "bearer", "secret": SECRET},
    }


def _post(client: TestClient, pid: str, base_url: str) -> Any:
    return client.post(f"/api/v1/projects/{pid}/apis", json=_body(base_url), headers=AUTH)


@pytest.mark.req("REQ-4.13-1", "REQ-4.13-3", "REQ-4.13-5")
@pytest.mark.parametrize(
    "base_url",
    [
        "https://169.254.169.254",
        "https://10.0.0.5",
        "https://localhost",
        "https://2130706433",
        "https://[::1]",
        "http://api.example.com",
        "https://private.example",
    ],
)
def test_a_blocked_destination_is_a_422_on_base_url_and_nothing_is_kept(base_url: str) -> None:
    client, services = _client({"api.example.com": [PUBLIC], "private.example": ["10.9.9.9"]})
    pid = _project(client)
    response = _post(client, pid, base_url)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert [d["loc"] for d in detail] == [["body", "base_url"]]
    assert detail[0]["msg"] == MESSAGE
    assert "10.9.9.9" not in response.text
    assert isinstance(services.apis, FakeApis)
    assert services.apis.rows == {}
    assert isinstance(services.secrets, FakeSecrets)
    assert services.secrets.rows == {}  # the sealed provider secret was deleted again


@pytest.mark.req("REQ-4.13-1")
def test_public_and_unresolvable_hosts_save() -> None:
    client, _ = _client({"api.example.com": [PUBLIC]})
    pid = _project(client)
    assert _post(client, pid, "https://api.example.com/v1").status_code == 201
    response = client.post(
        f"/api/v1/projects/{pid}/apis", json=_body("https://nx.example", "other"), headers=AUTH
    )
    assert response.status_code == 201


@pytest.mark.req("REQ-4.13-4", "REQ-4.13-5")
def test_an_allowlisted_internal_http_host_saves() -> None:
    client, _ = _client({"internal.example": ["10.1.2.3"]}, allowlist="internal.example")
    pid = _project(client)
    assert _post(client, pid, "http://internal.example:8080").status_code == 201


@pytest.mark.req("REQ-4.13-1")
def test_a_patch_to_a_blocked_host_is_rejected_and_the_version_is_unchanged() -> None:
    client, services = _client({"api.example.com": [PUBLIC]})
    pid = _project(client)
    created = _post(client, pid, "https://api.example.com").json()
    response = client.patch(
        f"/api/v1/apis/{created['id']}", json={"base_url": "https://10.0.0.5"}, headers=AUTH
    )
    assert response.status_code == 422
    assert [d["loc"] for d in response.json()["detail"]] == [["body", "base_url"]]
    current = client.get(f"/api/v1/apis/{created['id']}", headers=AUTH).json()
    assert current["version"] == created["version"]
    assert current["base_url"] == "https://api.example.com"


@pytest.mark.req("REQ-4.13-1")
def test_a_patch_that_keeps_a_public_host_still_works() -> None:
    client, _ = _client({"api.example.com": [PUBLIC]})
    pid = _project(client)
    created = _post(client, pid, "https://api.example.com").json()
    response = client.patch(
        f"/api/v1/apis/{created['id']}", json={"deadline_ms": 4000}, headers=AUTH
    )
    assert response.status_code == 200


def test_an_invalid_allowlist_stops_startup_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@postgres:5432/jarvis")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("JARVIS_SSRF_ALLOWLIST", "10.0.0.0/99")
    with pytest.raises(ValueError, match="JARVIS_SSRF_ALLOWLIST") as exc:
        create_app_from_env()
    assert "10.0.0.0/99" not in str(exc.value)


def test_the_env_app_builds_the_checker_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@postgres:5432/jarvis")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("JARVIS_SSRF_ALLOWLIST", "mock-upstream")
    seen: list[Services] = []
    real = Services

    def spy(*args: Any, **kwargs: Any) -> Services:
        seen.append(real(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr("jarvis_api.app.Services", spy)
    create_app_from_env()
    assert seen
    assert seen[0].destinations is not None
