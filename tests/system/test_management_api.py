"""The management API through the real compose stack (M1.3)."""

import httpx
import pytest

from tests.conftest import ADMIN_TOKEN, API_URL, MockUpstream

pytestmark = [pytest.mark.system, pytest.mark.usefixtures("compose_stack")]

PROVIDER_SECRET = "sk-system-test-provider-secret-value"  # sent once, must never come back
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.mark.req("REQ-10-1", "REQ-10-2", "REQ-10-4", "REQ-5.2-1", "REQ-11.2-4")
def test_project_api_and_key_lifecycle(mock_upstream: MockUpstream) -> None:
    with httpx.Client(base_url=f"{API_URL}/api/v1", timeout=10) as client:
        assert client.get("/projects").status_code == 401  # no token
        assert client.get("/projects", headers={"Authorization": "Bearer wrong"}).status_code == 401

        client.headers.update(AUTH)
        seen: list[str] = []

        project = client.post("/projects", json={"name": "System", "slug": "system-test"})
        assert project.status_code == 201, project.text
        seen.append(project.text)
        pid = project.json()["id"]

        created_api = client.post(
            f"/projects/{pid}/apis",
            json={
                "slug": "mock",
                "name": "Mock upstream",
                "base_url": "http://mock-upstream:8080",
                "credential": {"mode": "bearer", "secret": PROVIDER_SECRET},
            },
        )
        assert created_api.status_code == 201, created_api.text
        seen.append(created_api.text)
        api = created_api.json()
        assert api["credential"] == {
            "mode": "bearer",
            "header_name": None,
            "param_name": None,
            "has_secret": True,
        }

        created_key = client.post(f"/projects/{pid}/keys", json={"name": "ci"})
        assert created_key.status_code == 201, created_key.text
        key = created_key.json()
        assert key["key"].startswith("jv_live_")
        assert key["prefix"]
        assert key["prefix"] != key["key"]

        listed_keys = client.get(f"/projects/{pid}/keys")
        listed_apis = client.get(f"/projects/{pid}/apis")
        fetched_api = client.get(f"/apis/{api['id']}")
        listed_projects = client.get("/projects")
        for r in (listed_keys, listed_apis, fetched_api, listed_projects):
            assert r.status_code == 200, r.text
            seen.append(r.text)
        assert [k["id"] for k in listed_keys.json()] == [key["id"]]
        assert "key" not in listed_keys.json()[0]
        assert [a["id"] for a in listed_apis.json()] == [api["id"]]

        # The full key appears only in the creation response; the secret appears nowhere.
        assert all(key["key"] not in text for text in seen)
        assert all(PROVIDER_SECRET not in text for text in [*seen, created_key.text])

        patched = client.patch(f"/apis/{api['id']}", json={"deadline_ms": 4000})
        assert patched.status_code == 200, patched.text
        assert patched.json()["deadline_ms"] == 4000
        assert patched.json()["version"] == api["version"] + 1
        assert PROVIDER_SECRET not in patched.text

        assert client.delete(f"/keys/{key['id']}").status_code == 204
        revoked = client.get(f"/projects/{pid}/keys").json()[0]
        assert revoked["revoked_at"] is not None

        assert client.delete(f"/apis/{api['id']}").status_code == 204
        assert client.get(f"/apis/{api['id']}").status_code == 404
        assert client.delete(f"/projects/{pid}").status_code == 204

    # The api never called the upstream: it only stores configuration.
    assert mock_upstream.calls() == []


def test_openapi_is_served_only_with_the_token() -> None:
    assert httpx.get(f"{API_URL}/openapi.json", timeout=5).status_code == 401
    r = httpx.get(f"{API_URL}/openapi.json", headers=AUTH, timeout=5)
    assert r.status_code == 200
    assert "/api/v1/projects" in r.json()["paths"]
