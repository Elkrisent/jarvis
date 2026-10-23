"""Project endpoints of /api/v1 against fake ports."""

from __future__ import annotations

import pytest
from fakes import ADMIN_TOKEN, AUTH, RecordingPublisher, fake_services
from fastapi.testclient import TestClient
from jarvis_api.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(services=fake_services(), admin_token=ADMIN_TOKEN))


def _create(client: TestClient, slug: str = "alpha", name: str = "Alpha") -> dict[str, object]:
    response = client.post("/api/v1/projects", json={"name": name, "slug": slug}, headers=AUTH)
    assert response.status_code == 201
    body: dict[str, object] = response.json()
    return body


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_project_crud_round_trip(client: TestClient) -> None:
    created = _create(client)
    assert set(created) == {"id", "name", "slug", "created_at"}
    pid = created["id"]

    got = client.get(f"/api/v1/projects/{pid}", headers=AUTH)
    assert got.status_code == 200
    assert got.json() == created

    renamed = client.patch(f"/api/v1/projects/{pid}", json={"name": "Beta"}, headers=AUTH)
    assert renamed.status_code == 200
    assert (renamed.json()["name"], renamed.json()["slug"]) == ("Beta", "alpha")

    reslugged = client.patch(f"/api/v1/projects/{pid}", json={"slug": "beta"}, headers=AUTH)
    assert reslugged.json()["slug"] == "beta"
    assert client.get("/api/v1/projects", headers=AUTH).json() == [reslugged.json()]

    deleted = client.delete(f"/api/v1/projects/{pid}", headers=AUTH)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get(f"/api/v1/projects/{pid}", headers=AUTH).status_code == 404
    assert client.get("/api/v1/projects", headers=AUTH).json() == []


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
@pytest.mark.parametrize(
    ("method", "body"),
    [("get", None), ("patch", {"name": "x"}), ("delete", None)],
)
def test_unknown_project_is_404(client: TestClient, method: str, body: object) -> None:
    kwargs = {"json": body} if body is not None else {}
    response = client.request(method, "/api/v1/projects/prj_missing", headers=AUTH, **kwargs)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JV_NOT_FOUND"


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_patch_to_a_taken_slug_is_409(client: TestClient) -> None:
    _create(client, "alpha")
    other = _create(client, "beta", "Beta")
    response = client.patch(f"/api/v1/projects/{other['id']}", json={"slug": "alpha"}, headers=AUTH)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "JV_CONFLICT"


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({"name": "A", "slug": "Bad Slug"}, ["body", "slug"]),
        ({"name": "", "slug": "ok"}, ["body", "name"]),
        ({"name": "A" * 201, "slug": "ok"}, ["body", "name"]),
        ({"slug": "ok"}, ["body", "name"]),
        ({"name": "A", "slug": "ok", "id": "prj_x"}, ["body", "id"]),
    ],
)
def test_create_validation_errors_name_the_field(
    client: TestClient, body: dict[str, object], loc: list[str]
) -> None:
    response = client.post("/api/v1/projects", json=body, headers=AUTH)
    assert response.status_code == 422
    assert loc in [e["loc"] for e in response.json()["detail"]]


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({"slug": "Bad Slug"}, ["body", "slug"]),
        ({"name": ""}, ["body", "name"]),
        ({"created_at": "now"}, ["body", "created_at"]),
    ],
)
def test_patch_validation_errors_name_the_field(
    client: TestClient, body: dict[str, object], loc: list[str]
) -> None:
    pid = _create(client)["id"]
    response = client.patch(f"/api/v1/projects/{pid}", json=body, headers=AUTH)
    assert response.status_code == 422
    assert loc in [e["loc"] for e in response.json()["detail"]]


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_empty_patch_returns_the_project_unchanged(client: TestClient) -> None:
    created = _create(client)
    response = client.patch(f"/api/v1/projects/{created['id']}", json={}, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == created


@pytest.mark.unit
@pytest.mark.req("REQ-10-2")
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/projects/prj_x"),
        ("patch", "/api/v1/projects/prj_x"),
        ("delete", "/api/v1/projects/prj_x"),
    ],
)
def test_project_item_routes_need_the_token(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path).status_code == 401


@pytest.mark.unit
@pytest.mark.req("REQ-11.1-6")
def test_deleting_a_project_publishes_an_invalidation_only_on_success() -> None:
    services = fake_services()
    client = TestClient(create_app(services=services, admin_token=ADMIN_TOKEN))
    assert isinstance(services.publisher, RecordingPublisher)
    pid = str(_create(client)["id"])
    assert client.delete("/api/v1/projects/prj_missing", headers=AUTH).status_code == 404
    assert services.publisher.invalidations == []
    assert client.delete(f"/api/v1/projects/{pid}", headers=AUTH).status_code == 204
    assert services.publisher.invalidations == [("project", pid)]
