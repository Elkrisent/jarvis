"""API dependency endpoints of /api/v1 against fake ports, including provider secrets."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fakes import (
    ADMIN_TOKEN,
    AUTH,
    FakeApis,
    FakeSecrets,
    RecordingPublisher,
    fake_services,
)
from fastapi.testclient import TestClient
from httpx import Response
from jarvis_api.app import create_app
from jarvis_api.db.config_source import StoredApi
from jarvis_api.http.apis import config_diff, merge_patch
from jarvis_api.http.deps import Services
from jarvis_core.config import ApiDependency

SECRET = "SENTINEL-SECRET-1"


@pytest.fixture
def services() -> Services:
    return fake_services()


@pytest.fixture
def client(services: Services) -> TestClient:
    return TestClient(create_app(services=services, admin_token=ADMIN_TOKEN))


def _secrets(services: Services) -> FakeSecrets:
    assert isinstance(services.secrets, FakeSecrets)
    return services.secrets


def _apis(services: Services) -> FakeApis:
    assert isinstance(services.apis, FakeApis)
    return services.apis


def _published(services: Services) -> list[tuple[str, int]]:
    assert isinstance(services.publisher, RecordingPublisher)
    return services.publisher.calls


def _project(client: TestClient, slug: str = "alpha") -> str:
    response = client.post("/api/v1/projects", json={"name": slug, "slug": slug}, headers=AUTH)
    assert response.status_code == 201
    pid: str = response.json()["id"]
    return pid


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "slug": "weather",
        "name": "Weather",
        "base_url": "https://api.weather.example",
        "credential": {"mode": "bearer", "secret": SECRET},
    }
    body.update(overrides)
    return body


def _create(client: TestClient, pid: str, **overrides: Any) -> dict[str, Any]:
    response = client.post(f"/api/v1/projects/{pid}/apis", json=_body(**overrides), headers=AUTH)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def _patch(client: TestClient, api_id: str, body: object) -> Response:
    return client.patch(f"/api/v1/apis/{api_id}", json=body, headers=AUTH)


def _ref(services: Services, api_id: str) -> str | None:
    return _apis(services).rows[api_id].config.credential.secret_ref


# --- create, read, list --------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_create_returns_the_api_with_has_secret_only(
    client: TestClient, services: Services
) -> None:
    pid = _project(client)
    created = _create(client, pid)
    assert created["id"].startswith("api_")
    assert (created["project_id"], created["version"]) == (pid, 1)
    assert created["slug"] == "weather"
    assert created["deadline_ms"] == 5000
    assert created["credential"] == {
        "mode": "bearer",
        "header_name": None,
        "param_name": None,
        "has_secret": True,
    }
    assert "created_at" in created
    assert "reason" not in created
    ref = _ref(services, created["id"])
    assert ref is not None
    assert _secrets(services).rows == {ref: SECRET}
    assert _published(services) == [(created["id"], 1)]


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_get_and_list_show_the_current_version(client: TestClient) -> None:
    pid = _project(client)
    created = _create(client, pid)
    got = client.get(f"/api/v1/apis/{created['id']}", headers=AUTH)
    assert got.status_code == 200
    assert got.json() == created
    listed = client.get(f"/api/v1/projects/{pid}/apis", headers=AUTH)
    assert listed.status_code == 200
    assert listed.json() == [created]


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_api_without_a_credential_stores_no_secret(client: TestClient, services: Services) -> None:
    pid = _project(client)
    body = _body()
    del body["credential"]
    response = client.post(f"/api/v1/projects/{pid}/apis", json=body, headers=AUTH)
    assert response.status_code == 201
    assert response.json()["credential"]["has_secret"] is False
    assert _secrets(services).rows == {}


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_header_and_query_credentials_round_trip(client: TestClient) -> None:
    pid = _project(client)
    header = _create(
        client, pid, slug="h", credential={"mode": "header", "header_name": "X-Key", "secret": "s"}
    )
    assert header["credential"]["header_name"] == "X-Key"
    query = _create(
        client, pid, slug="q", credential={"mode": "query", "param_name": "key", "secret": "s"}
    )
    assert query["credential"]["param_name"] == "key"


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_create_with_a_taken_slug_is_409_and_stores_nothing(
    client: TestClient, services: Services
) -> None:
    pid = _project(client)
    _create(client, pid)
    response = client.post(f"/api/v1/projects/{pid}/apis", json=_body(), headers=AUTH)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "JV_CONFLICT"
    assert len(_secrets(services).rows) == 1
    assert len(_published(services)) == 1


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_same_slug_in_another_project_is_fine(client: TestClient) -> None:
    _create(client, _project(client, "alpha"))
    _create(client, _project(client, "beta"))


# --- validation ----------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
@pytest.mark.parametrize(
    ("overrides", "loc"),
    [
        ({"retry": {"max_attempts": 0}}, ["body", "retry", "max_attempts"]),
        ({"attempt_timeout_ms": 6000, "deadline_ms": 5000}, ["body"]),
        ({"credential": {"mode": "bearer"}}, ["body", "credential"]),
        ({"credential": {"mode": "none", "secret": SECRET}}, ["body", "credential"]),
        ({"credential": {"mode": "bearer", "secret": ""}}, ["body", "credential", "secret"]),
        ({"credential": {"mode": "bearer", "secret_ref": "sec_x"}}, ["body", "credential"]),
        ({"credential": {"mode": "header", "secret": "s"}}, ["body", "credential"]),
        ({"credential": {"mode": "query", "secret": "s"}}, ["body", "credential"]),
        (
            {"credential": {"mode": "bearer", "secret": "s", "header_name": "X-K"}},
            ["body", "credential"],
        ),
        (
            {"credential": {"mode": "bearer", "secret": "s", "param_name": "k"}},
            ["body", "credential"],
        ),
        ({"slug": "Bad Slug"}, ["body", "slug"]),
        ({"base_url": "ftp://x"}, ["body", "base_url"]),
        ({"version": 3}, ["body", "version"]),
    ],
)
def test_create_validation_errors_name_the_field(
    client: TestClient, services: Services, overrides: dict[str, Any], loc: list[str]
) -> None:
    pid = _project(client)
    response = client.post(f"/api/v1/projects/{pid}/apis", json=_body(**overrides), headers=AUTH)
    assert response.status_code == 422, response.text
    locs = [e["loc"] for e in response.json()["detail"]]
    assert any(found[: len(loc)] == loc for found in locs), locs
    assert SECRET not in response.text
    assert _secrets(services).rows == {}
    assert _published(services) == []


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_create_rejects_secret_ref_from_the_client(client: TestClient) -> None:
    pid = _project(client)
    body = _body(credential={"mode": "bearer", "secret": SECRET, "secret_ref": "sec_x"})
    response = client.post(f"/api/v1/projects/{pid}/apis", json=body, headers=AUTH)
    assert response.status_code == 422
    assert ["body", "credential", "secret_ref"] in [e["loc"] for e in response.json()["detail"]]


# --- not found ------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_unknown_project_is_404(client: TestClient, services: Services) -> None:
    created = client.post("/api/v1/projects/prj_missing/apis", json=_body(), headers=AUTH)
    assert created.status_code == 404
    assert _secrets(services).rows == {}
    assert client.get("/api/v1/projects/prj_missing/apis", headers=AUTH).status_code == 404


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("get", "", None),
        ("patch", "", {"deadline_ms": 1}),
        ("delete", "", None),
        ("post", "/test", None),
    ],
)
def test_unknown_api_is_404(client: TestClient, method: str, suffix: str, body: object) -> None:
    kwargs = {"json": body} if body is not None else {}
    response = client.request(method, f"/api/v1/apis/api_missing{suffix}", headers=AUTH, **kwargs)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JV_NOT_FOUND"


# --- PATCH: merge patch and versions ----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.req("REQ-5.2-3")
def test_patch_creates_the_next_version_and_publishes_it(
    client: TestClient, services: Services
) -> None:
    pid = _project(client)
    created = _create(client, pid)
    response = _patch(client, created["id"], {"deadline_ms": 8000, "reason": "slow upstream"})
    assert response.status_code == 200
    patched = response.json()
    assert (patched["version"], patched["deadline_ms"]) == (2, 8000)
    assert patched["retry"] == created["retry"]
    assert _published(services) == [(created["id"], 1), (created["id"], 2)]
    assert _apis(services).reasons == [None, "slow upstream"]


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_patch_merges_nested_objects_and_null_resets(client: TestClient) -> None:
    pid = _project(client)
    created = _create(client, pid, retry={"max_attempts": 5, "base_ms": 200})
    merged = _patch(client, created["id"], {"retry": {"max_attempts": 2}}).json()
    assert (merged["retry"]["max_attempts"], merged["retry"]["base_ms"]) == (2, 200)
    reset = _patch(client, created["id"], {"retry": None}).json()
    assert reset["retry"]["max_attempts"] == 3
    assert reset["retry"]["base_ms"] == 100


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_patch_can_change_the_slug_but_not_to_a_taken_one(client: TestClient) -> None:
    pid = _project(client)
    first = _create(client, pid)
    second = _create(client, pid, slug="maps", name="Maps")
    renamed = _patch(client, second["id"], {"slug": "geo"})
    assert renamed.status_code == 200
    assert renamed.json()["slug"] == "geo"
    clash = _patch(client, second["id"], {"slug": first["slug"]})
    assert clash.status_code == 409


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
@pytest.mark.parametrize(
    ("patch", "loc"),
    [
        ({"retry": {"max_attempts": 0}}, ["body", "retry", "max_attempts"]),
        ({"attempt_timeout_ms": 99999}, ["body"]),
        ({"credential": {"secret_ref": "sec_other"}}, ["body", "credential", "secret_ref"]),
        ({"credential": {"has_secret": False}}, ["body", "credential", "has_secret"]),
        ({"id": "api_other"}, ["body", "id"]),
        ({"slug": None}, ["body", "slug"]),
        ([1, 2], ["body"]),
        ("text", ["body"]),
    ],
)
def test_patch_validation_errors_name_the_field(
    client: TestClient, services: Services, patch: object, loc: list[str]
) -> None:
    pid = _project(client)
    created = _create(client, pid)
    response = _patch(client, created["id"], patch)
    assert response.status_code == 422, response.text
    locs = [e["loc"] for e in response.json()["detail"]]
    assert any(found[: len(loc)] == loc for found in locs), locs
    assert all(set(e) == {"type", "loc", "msg"} for e in response.json()["detail"])
    assert _apis(services).rows[created["id"]].version == 1


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_patch_with_malformed_json_is_422_without_echo(client: TestClient) -> None:
    pid = _project(client)
    created = _create(client, pid)
    response = client.patch(
        f"/api/v1/apis/{created['id']}",
        content=b'{"x": SENTINEL-SECRET-1',
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert SECRET not in response.text


# --- PATCH: secret semantics ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-4")
def test_patch_without_a_secret_keeps_the_stored_one(
    client: TestClient, services: Services
) -> None:
    created = _create(client, _project(client))
    before = _ref(services, created["id"])
    response = _patch(client, created["id"], {"credential": {"mode": "bearer"}, "name": "W2"})
    assert response.status_code == 200
    assert response.json()["credential"]["has_secret"] is True
    assert _ref(services, created["id"]) == before
    assert _secrets(services).rows == {before: SECRET}


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-4")
def test_patch_with_a_new_secret_replaces_and_deletes_the_old(
    client: TestClient, services: Services
) -> None:
    created = _create(client, _project(client))
    before = _ref(services, created["id"])
    response = _patch(client, created["id"], {"credential": {"secret": "SENTINEL-SECRET-2"}})
    assert response.status_code == 200
    after = _ref(services, created["id"])
    assert after not in (None, before)
    assert _secrets(services).rows == {after: "SENTINEL-SECRET-2"}
    assert "SENTINEL-SECRET-2" not in response.text


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-4")
def test_patch_to_mode_none_deletes_the_secret(client: TestClient, services: Services) -> None:
    created = _create(client, _project(client))
    response = _patch(client, created["id"], {"credential": {"mode": "none"}})
    assert response.status_code == 200
    assert response.json()["credential"] == {
        "mode": "none",
        "header_name": None,
        "param_name": None,
        "has_secret": False,
    }
    assert _ref(services, created["id"]) is None
    assert _secrets(services).rows == {}


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-4")
def test_patch_null_credential_resets_it_and_deletes_the_secret(
    client: TestClient, services: Services
) -> None:
    created = _create(client, _project(client))
    response = _patch(client, created["id"], {"credential": None})
    assert response.status_code == 200
    assert response.json()["credential"]["mode"] == "none"
    assert _secrets(services).rows == {}


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-4")
def test_patch_to_a_secret_mode_without_a_secret_is_422(
    client: TestClient, services: Services
) -> None:
    pid = _project(client)
    body = _body()
    del body["credential"]
    created = client.post(f"/api/v1/projects/{pid}/apis", json=body, headers=AUTH).json()
    response = _patch(client, created["id"], {"credential": {"mode": "bearer"}})
    assert response.status_code == 422
    assert ["body", "credential"] in [e["loc"] for e in response.json()["detail"]]
    added = _patch(client, created["id"], {"credential": {"mode": "bearer", "secret": SECRET}})
    assert added.status_code == 200
    assert added.json()["credential"]["has_secret"] is True
    assert list(_secrets(services).rows.values()) == [SECRET]


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-4")
def test_patch_null_secret_with_a_secret_mode_is_422(client: TestClient) -> None:
    created = _create(client, _project(client))
    response = _patch(client, created["id"], {"credential": {"secret": None}})
    assert response.status_code == 422
    assert ["body", "credential"] in [e["loc"] for e in response.json()["detail"]]


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-4")
def test_patch_with_a_secret_and_mode_none_is_422(client: TestClient, services: Services) -> None:
    created = _create(client, _project(client))
    response = _patch(client, created["id"], {"credential": {"mode": "none", "secret": "x"}})
    assert response.status_code == 422
    assert len(_secrets(services).rows) == 1


# --- failed saves leave no orphan secret --------------------------------------------------


class _Boom(Exception):
    pass


def _failing_put(services: Services, exc: Exception) -> None:
    async def put_version(*args: Any, **kwargs: Any) -> tuple[str, int]:
        raise exc

    services.apis.put_version = put_version  # type: ignore[method-assign]


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-4")
def test_failed_create_deletes_the_new_secret(services: Services) -> None:
    client = TestClient(
        create_app(services=services, admin_token=ADMIN_TOKEN), raise_server_exceptions=False
    )
    pid = _project(client)
    _failing_put(services, _Boom("db down"))
    response = client.post(f"/api/v1/projects/{pid}/apis", json=_body(), headers=AUTH)
    assert response.status_code == 500
    assert _secrets(services).rows == {}
    assert _published(services) == []


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-4")
def test_failed_patch_deletes_the_new_secret_and_keeps_the_old(
    client: TestClient, services: Services
) -> None:
    created = _create(client, _project(client))
    before = _ref(services, created["id"])
    second = _create(client, created["project_id"], slug="maps", name="Maps")
    response = _patch(client, second["id"], {"slug": "weather", "credential": {"secret": "new"}})
    assert response.status_code == 409
    assert before in _secrets(services).rows
    assert "new" not in _secrets(services).rows.values()
    assert len(_secrets(services).rows) == 2


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-4")
def test_patch_from_a_stale_read_is_409_and_keeps_the_newer_save(
    client: TestClient, services: Services
) -> None:
    created = _create(client, _project(client))
    api_id = created["id"]
    stale = _apis(services).rows[api_id]
    assert (
        _patch(client, api_id, {"credential": {"secret": "SENTINEL-SECRET-2"}}).status_code == 200
    )
    newer_ref = _ref(services, api_id)

    async def stale_read(_: str) -> StoredApi:
        return stale

    services.apis.get_by_id = stale_read  # type: ignore[method-assign]
    response = _patch(client, api_id, {"deadline_ms": 9000, "credential": {"secret": "x"}})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "JV_CONFLICT"
    current = _apis(services).rows[api_id]
    assert (current.version, current.config.deadline_ms) == (2, 5000)
    assert _secrets(services).rows == {newer_ref: "SENTINEL-SECRET-2"}
    assert _published(services) == [(api_id, 1), (api_id, 2)]


def _failing_secret_delete(services: Services) -> None:
    async def delete(secret_id: str) -> None:
        raise OSError(f"cannot reach the database to delete {secret_id}")

    services.secrets.delete = delete  # type: ignore[method-assign]


@pytest.mark.unit
@pytest.mark.req("REQ-5.2-3")
def test_patch_succeeds_and_publishes_when_the_old_secret_cannot_be_deleted(
    client: TestClient, services: Services, caplog: pytest.LogCaptureFixture
) -> None:
    created = _create(client, _project(client))
    old_ref = _ref(services, created["id"])
    assert old_ref is not None
    _failing_secret_delete(services)
    with caplog.at_level(logging.WARNING, logger="jarvis_api.http.apis"):
        response = _patch(client, created["id"], {"credential": {"secret": "SENTINEL-SECRET-2"}})
    assert response.status_code == 200
    assert _published(services) == [(created["id"], 1), (created["id"], 2)]
    assert "provider secret" in caplog.text
    assert old_ref not in caplog.text
    assert "SENTINEL" not in caplog.text


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_delete_succeeds_when_the_secret_cannot_be_deleted(
    client: TestClient, services: Services, caplog: pytest.LogCaptureFixture
) -> None:
    created = _create(client, _project(client))
    _failing_secret_delete(services)
    with caplog.at_level(logging.WARNING, logger="jarvis_api.http.apis"):
        response = client.delete(f"/api/v1/apis/{created['id']}", headers=AUTH)
    assert response.status_code == 204
    assert "sec_" not in caplog.text


# --- delete and test ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-4")
def test_delete_removes_the_api_and_its_secret(client: TestClient, services: Services) -> None:
    created = _create(client, _project(client))
    response = client.delete(f"/api/v1/apis/{created['id']}", headers=AUTH)
    assert response.status_code == 204
    assert response.content == b""
    assert client.get(f"/api/v1/apis/{created['id']}", headers=AUTH).status_code == 404
    assert _secrets(services).rows == {}


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_delete_of_an_api_without_a_secret(client: TestClient, services: Services) -> None:
    pid = _project(client)
    keep = _create(client, pid, slug="keep")
    body = _body(slug="plain")
    del body["credential"]
    plain = client.post(f"/api/v1/projects/{pid}/apis", json=body, headers=AUTH).json()
    assert client.delete(f"/api/v1/apis/{plain['id']}", headers=AUTH).status_code == 204
    assert list(_secrets(services).rows) == [_ref(services, keep["id"])]


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_test_endpoint_is_a_501_placeholder(client: TestClient) -> None:
    created = _create(client, _project(client))
    response = client.post(f"/api/v1/apis/{created['id']}/test", headers=AUTH)
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "JV_NOT_IMPLEMENTED"
    assert "M4.10" in response.json()["error"]["message"]


@pytest.mark.unit
@pytest.mark.req("REQ-10-2")
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/projects/prj_x/apis"),
        ("post", "/api/v1/projects/prj_x/apis"),
        ("get", "/api/v1/apis/api_x"),
        ("patch", "/api/v1/apis/api_x"),
        ("delete", "/api/v1/apis/api_x"),
        ("post", "/api/v1/apis/api_x/test"),
    ],
)
def test_api_routes_need_the_token(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path).status_code == 401


# --- the secret never leaks -------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-4")
def test_the_secret_appears_in_no_response_and_no_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    pid = _project(client)
    created_response = client.post(f"/api/v1/projects/{pid}/apis", json=_body(), headers=AUTH)
    api_id = created_response.json()["id"]
    responses = [
        created_response,
        client.get(f"/api/v1/apis/{api_id}", headers=AUTH),
        client.get(f"/api/v1/projects/{pid}/apis", headers=AUTH),
        _patch(client, api_id, {"credential": {"secret": SECRET}, "deadline_ms": 6000}),
        _patch(client, api_id, {"credential": {"secret": SECRET}, "retry": {"max_attempts": 0}}),
        _patch(client, api_id, {"credential": {"secret": SECRET, "mode": "none"}}),
        client.post(
            f"/api/v1/projects/{pid}/apis",
            json=_body(slug="x", retry={"max_attempts": 0}),
            headers=AUTH,
        ),
        client.post(
            f"/api/v1/projects/{pid}/apis",
            json=_body(slug="x", credential={"mode": "none", "secret": SECRET}),
            headers=AUTH,
        ),
        client.post(f"/api/v1/projects/{pid}/apis", json=_body(), headers=AUTH),  # 409
    ]
    assert [r.status_code for r in responses] == [201, 200, 200, 200, 422, 422, 422, 422, 409]
    for response in responses:
        assert SECRET not in response.text
        assert "secret_ref" not in response.text
        assert "sec_" not in response.text
    assert SECRET not in caplog.text


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-4")
def test_openapi_documents_the_secret_as_write_only_and_hides_refs(client: TestClient) -> None:
    schema = client.get("/openapi.json", headers=AUTH).json()
    components = schema["components"]["schemas"]
    secret = components["CredentialIn"]["properties"]["secret"]
    assert "example" not in secret
    assert "examples" not in secret
    assert "secret_ref" not in components["CredentialIn"]["properties"]
    assert set(components["CredentialOut"]["properties"]) == {
        "mode",
        "header_name",
        "param_name",
        "has_secret",
    }
    assert "secret" not in str(components["ApiOut"]["properties"])
    patch_op = schema["paths"]["/api/v1/apis/{api_id}"]["patch"]
    assert "ApiIn" in str(patch_op["requestBody"])
    assert SECRET not in str(schema)


# --- pure helpers ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_merge_patch_follows_rfc_7386() -> None:
    target = {"a": "b", "c": {"d": "e", "f": "g"}}
    assert merge_patch(target, {"a": "z", "c": {"f": None}}) == {"a": "z", "c": {"d": "e"}}
    assert merge_patch(target, {"c": None}) == {"a": "b"}
    assert merge_patch(target, {"n": {"x": None, "y": 1}}) == {**target, "n": {"y": 1}}
    assert merge_patch({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}
    assert merge_patch({"a": {"b": 1}}, {"a": "s"}) == {"a": "s"}
    assert merge_patch({"a": "s"}, {"a": {"b": 1}}) == {"a": {"b": 1}}
    assert merge_patch(target, ["x"]) == ["x"]
    assert target == {"a": "b", "c": {"d": "e", "f": "g"}}


def _stored(**changes: Any) -> dict[str, Any]:
    api = ApiDependency.model_validate(
        {
            "slug": "w",
            "name": "W",
            "base_url": "https://w.example",
            "credential": {"mode": "bearer", "secret_ref": "sec_1"},
            **changes,
        }
    )
    return api.model_dump(mode="json")


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_config_diff_reports_top_level_keys_without_secret_refs() -> None:
    old = _stored()
    assert config_diff(None, old) == {"created": True}
    assert config_diff(old, old) == {"changed": {}}
    new = _stored(retry={"max_attempts": 5}, credential={"mode": "bearer", "secret_ref": "sec_2"})
    diff = config_diff(old, new)
    assert set(diff["changed"]) == {"retry"}
    assert diff["changed"]["retry"]["to"]["max_attempts"] == 5
    assert "sec_" not in str(config_diff(old, _stored(credential={"mode": "none"})))
    unchanged_ref = config_diff(old, _stored(credential={"mode": "bearer", "secret_ref": "sec_9"}))
    assert unchanged_ref == {"changed": {}}
