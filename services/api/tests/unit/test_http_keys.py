"""JARVIS key endpoints of /api/v1 against fake ports."""

from __future__ import annotations

import pytest
from fakes import ADMIN_TOKEN, AUTH, FakeKeys, RecordingPublisher, fake_services
from fastapi.testclient import TestClient
from jarvis_api.app import create_app
from jarvis_api.http.deps import Services
from jarvis_core.config import ApiDependency
from jarvis_core.keys import KEY_PREFIX, hash_jarvis_key


@pytest.fixture
def services() -> Services:
    return fake_services()


@pytest.fixture
def client(services: Services) -> TestClient:
    return TestClient(create_app(services=services, admin_token=ADMIN_TOKEN))


def _project(client: TestClient, slug: str = "alpha") -> str:
    response = client.post("/api/v1/projects", json={"name": slug, "slug": slug}, headers=AUTH)
    assert response.status_code == 201
    pid: str = response.json()["id"]
    return pid


async def _api(services: Services, project_id: str, slug: str = "weather") -> str:
    api = ApiDependency(slug=slug, name=slug, base_url="https://x.example")
    api_id, _ = await services.apis.put_version(project_id, api)
    return api_id


@pytest.mark.unit
@pytest.mark.req("REQ-10-1", "REQ-10-4")
def test_created_key_is_shown_once_and_only_its_prefix_is_listed(
    client: TestClient, services: Services
) -> None:
    pid = _project(client)
    created = client.post(f"/api/v1/projects/{pid}/keys", json={"name": "ci"}, headers=AUTH)
    assert created.status_code == 201
    body = created.json()
    assert set(body) == {
        "id",
        "project_id",
        "name",
        "prefix",
        "allowed_api_ids",
        "revoked_at",
        "key",
    }
    key = body["key"]
    assert key.startswith(KEY_PREFIX)
    assert body["prefix"] == key[:12]
    assert (body["project_id"], body["name"], body["allowed_api_ids"]) == (pid, "ci", [])
    assert body["revoked_at"] is None
    assert isinstance(services.keys, FakeKeys)
    assert services.keys.hashes[body["id"]] == hash_jarvis_key(key)

    listed = client.get(f"/api/v1/projects/{pid}/keys", headers=AUTH)
    assert listed.status_code == 200
    [item] = listed.json()
    assert "key" not in item
    assert "hash" not in item
    assert item == {k: v for k, v in body.items() if k != "key"}
    assert key not in listed.text
    assert hash_jarvis_key(key) not in listed.text
    assert hash_jarvis_key(key) not in created.text


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_each_key_is_different(client: TestClient) -> None:
    pid = _project(client)
    keys = {
        client.post(f"/api/v1/projects/{pid}/keys", json={"name": "k"}, headers=AUTH).json()["key"]
        for _ in range(3)
    }
    assert len(keys) == 3


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
async def test_key_can_be_restricted_to_the_projects_apis(
    client: TestClient, services: Services
) -> None:
    pid = _project(client)
    api_id = await _api(services, pid)
    response = client.post(
        f"/api/v1/projects/{pid}/keys",
        json={"name": "k", "allowed_api_ids": [api_id]},
        headers=AUTH,
    )
    assert response.status_code == 201
    assert response.json()["allowed_api_ids"] == [api_id]


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
async def test_key_restricted_to_another_projects_api_is_422(
    client: TestClient, services: Services
) -> None:
    pid = _project(client, "alpha")
    other_api = await _api(services, _project(client, "beta"))
    for allowed in ([other_api], ["api_missing"]):
        response = client.post(
            f"/api/v1/projects/{pid}/keys",
            json={"name": "k", "allowed_api_ids": allowed},
            headers=AUTH,
        )
        assert response.status_code == 422
        [error] = response.json()["detail"]
        assert error["loc"] == ["body", "allowed_api_ids"]
    assert client.get(f"/api/v1/projects/{pid}/keys", headers=AUTH).json() == []


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({}, ["body", "name"]),
        ({"name": ""}, ["body", "name"]),
        ({"name": "k", "allowed_api_ids": "api_x"}, ["body", "allowed_api_ids"]),
        ({"name": "k", "hash": "abc"}, ["body", "hash"]),
    ],
)
def test_key_validation_errors_name_the_field(
    client: TestClient, body: dict[str, object], loc: list[str]
) -> None:
    pid = _project(client)
    response = client.post(f"/api/v1/projects/{pid}/keys", json=body, headers=AUTH)
    assert response.status_code == 422
    assert loc in [e["loc"] for e in response.json()["detail"]]


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_keys_of_an_unknown_project_are_404(client: TestClient) -> None:
    created = client.post("/api/v1/projects/prj_missing/keys", json={"name": "k"}, headers=AUTH)
    assert created.status_code == 404
    listed = client.get("/api/v1/projects/prj_missing/keys", headers=AUTH)
    assert listed.status_code == 404
    assert listed.json()["error"]["code"] == "JV_NOT_FOUND"


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_revoking_a_key_keeps_it_with_revoked_at(client: TestClient) -> None:
    pid = _project(client)
    key_id = client.post(f"/api/v1/projects/{pid}/keys", json={"name": "k"}, headers=AUTH).json()[
        "id"
    ]
    response = client.delete(f"/api/v1/keys/{key_id}", headers=AUTH)
    assert response.status_code == 204
    assert response.content == b""
    [item] = client.get(f"/api/v1/projects/{pid}/keys", headers=AUTH).json()
    assert item["id"] == key_id
    assert item["revoked_at"] is not None
    assert client.delete(f"/api/v1/keys/{key_id}", headers=AUTH).status_code == 204


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_revoking_an_unknown_key_is_404(client: TestClient) -> None:
    response = client.delete("/api/v1/keys/key_missing", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JV_NOT_FOUND"


@pytest.mark.unit
@pytest.mark.req("REQ-10-2")
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/projects/prj_x/keys"),
        ("post", "/api/v1/projects/prj_x/keys"),
        ("delete", "/api/v1/keys/key_x"),
    ],
)
def test_key_routes_need_the_token(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path).status_code == 401


def _invalidations(services: Services) -> list[tuple[str, str]]:
    assert isinstance(services.publisher, RecordingPublisher)
    return services.publisher.invalidations


@pytest.mark.unit
@pytest.mark.req("REQ-11.1-6")
def test_creating_and_revoking_a_key_publish_invalidations(
    client: TestClient, services: Services
) -> None:
    pid = _project(client)
    key_id = client.post(f"/api/v1/projects/{pid}/keys", json={"name": "k"}, headers=AUTH).json()[
        "id"
    ]
    assert _invalidations(services) == [("key", key_id)]
    assert client.delete(f"/api/v1/keys/{key_id}", headers=AUTH).status_code == 204
    assert client.delete(f"/api/v1/keys/{key_id}", headers=AUTH).status_code == 204
    assert _invalidations(services) == [("key", key_id)] * 3


@pytest.mark.unit
@pytest.mark.req("REQ-11.1-6")
async def test_failed_key_operations_publish_nothing(
    client: TestClient, services: Services
) -> None:
    pid = _project(client)
    other = _project(client, "beta")
    foreign = await _api(services, other)
    assert (
        client.post(f"/api/v1/projects/{pid}/keys", json={"name": ""}, headers=AUTH).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/projects/{pid}/keys",
            json={"name": "k", "allowed_api_ids": [foreign]},
            headers=AUTH,
        ).status_code
        == 422
    )
    assert (
        client.post("/api/v1/projects/prj_x/keys", json={"name": "k"}, headers=AUTH).status_code
        == 404
    )
    assert client.delete("/api/v1/keys/key_missing", headers=AUTH).status_code == 404
    assert _invalidations(services) == []
