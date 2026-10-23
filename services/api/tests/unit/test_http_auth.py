"""Admin-token auth, uniform errors and validation-error hygiene for /api/v1."""

from __future__ import annotations

import base64

import pytest
from fakes import FakeApis, FakeKeys, FakeProjects, FakeSecrets, RecordingPublisher
from fastapi.testclient import TestClient
from jarvis_api import app as app_module
from jarvis_api.app import create_app, create_app_from_env
from jarvis_api.events import RedisConfigPublisher
from jarvis_api.http.deps import Services
from jarvis_api.settings import SettingsError, load_settings
from sqlalchemy.ext.asyncio import AsyncEngine

TOKEN = "s3cret-admin-token-" + "x" * 20
MASTER = base64.b64encode(bytes(range(32))).decode()
ENV = {
    "DATABASE_URL": "postgresql://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "JARVIS_ADMIN_TOKEN": TOKEN,
    "JARVIS_MASTER_KEY": MASTER,
}


def _services() -> Services:
    projects = FakeProjects()
    return Services(
        projects=projects,
        keys=FakeKeys(),
        apis=FakeApis(projects),
        secrets=FakeSecrets(),
        secret_box=load_settings(ENV).secret_box,
        publisher=RecordingPublisher(),
    )


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(services=_services(), admin_token=TOKEN))


BAD_HEADERS = [
    pytest.param({}, id="no-header"),
    pytest.param({"Authorization": "Basic abc"}, id="basic"),
    pytest.param({"Authorization": "Bearer "}, id="empty-bearer"),
    pytest.param({"Authorization": f"Bearer {TOKEN} "}, id="trailing-space"),
    pytest.param({"Authorization": f"Bearer {TOKEN[:-1]}"}, id="truncated"),
    pytest.param({"Authorization": f"Bearer {TOKEN}x"}, id="extended"),
    pytest.param({"Authorization": TOKEN}, id="no-scheme"),
]


@pytest.mark.unit
@pytest.mark.req("REQ-10-2")
@pytest.mark.parametrize("headers", BAD_HEADERS)
def test_bad_credentials_get_one_identical_401(client: TestClient, headers: dict[str, str]) -> None:
    reference = client.get("/api/v1/projects")
    response = client.get("/api/v1/projects", headers=headers)
    assert response.status_code == 401
    assert response.json() == reference.json()
    assert response.json()["error"]["code"] == "JV_AUTH_REQUIRED"
    assert set(response.json()["error"]) == {"code", "message"}


@pytest.mark.unit
@pytest.mark.req("REQ-10-2")
@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_scheme_is_case_insensitive(client: TestClient, scheme: str) -> None:
    response = client.get("/api/v1/projects", headers={"Authorization": f"{scheme} {TOKEN}"})
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.unit
@pytest.mark.req("REQ-10-2")
def test_non_ascii_credentials_are_rejected_not_crashed(client: TestClient) -> None:
    response = client.get(
        "/api/v1/projects", headers={"Authorization": "Bearer tökén".encode("latin-1")}
    )
    assert response.status_code == 401


@pytest.mark.unit
@pytest.mark.req("REQ-10-2")
def test_auth_is_checked_before_the_body(client: TestClient) -> None:
    response = client.post("/api/v1/projects", json={"bad": 1})
    assert response.status_code == 401


@pytest.mark.unit
@pytest.mark.req("REQ-10-2")
@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_probes_need_no_token(path: str) -> None:
    app = create_app(services=_services(), admin_token=TOKEN)
    assert TestClient(app).get(path).status_code in (200, 503)


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_docs_are_disabled_and_openapi_needs_the_token(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 401
    authed = client.get("/openapi.json", headers={"Authorization": f"Bearer {TOKEN}"})
    assert authed.status_code == 200
    assert "/api/v1/projects" in authed.json()["paths"]


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_api_is_mounted_under_v1_only_when_services_are_given() -> None:
    assert TestClient(create_app()).get("/api/v1/projects").status_code == 404


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_services_without_a_token_is_refused() -> None:
    with pytest.raises(ValueError, match="admin_token"):
        create_app(services=_services())


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_validation_errors_keep_loc_but_never_echo_input(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    body = {"name": "ok", "slug": "BAD SLUG SENTINEL-SECRET", "extra": "SENTINEL-SECRET"}
    response = client.post("/api/v1/projects", json=body, headers=headers)
    assert response.status_code == 422
    assert "SENTINEL-SECRET" not in response.text
    detail = response.json()["detail"]
    assert ["body", "slug"] in [e["loc"] for e in detail]
    assert ["body", "extra"] in [e["loc"] for e in detail]
    assert all("input" not in e and "ctx" not in e for e in detail)


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_malformed_json_does_not_echo_the_body(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    response = client.post("/api/v1/projects", content=b'{"x": SENTINEL-SECRET', headers=headers)
    assert response.status_code == 422
    assert "SENTINEL-SECRET" not in response.text


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_unknown_route_and_method_use_the_error_format(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    missing = client.get("/api/v1/nope", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "JV_NOT_FOUND"
    wrong = client.patch("/api/v1/projects", headers=headers)
    assert wrong.status_code == 405
    assert wrong.json()["error"]["code"] == "JV_NOT_IMPLEMENTED"


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_conflict_maps_to_409(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    first = client.post("/api/v1/projects", json={"name": "A", "slug": "a"}, headers=headers)
    assert first.status_code == 201
    again = client.post("/api/v1/projects", json={"name": "B", "slug": "a"}, headers=headers)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "JV_CONFLICT"


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_not_found_from_a_port_maps_to_404() -> None:
    from jarvis_api.db.repositories import NotFound

    services = _services()

    async def boom() -> list[object]:
        raise NotFound("project not found: prj_x")

    services.projects.list = boom  # type: ignore[method-assign,assignment]
    client = TestClient(create_app(services=services, admin_token=TOKEN))
    response = client.get("/api/v1/projects", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "JV_NOT_FOUND",
        "message": "project not found: prj_x",
    }


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_shutdown_hook_runs_when_the_app_stops() -> None:
    calls: list[str] = []

    async def shutdown() -> None:
        calls.append("down")

    with TestClient(create_app(services=_services(), admin_token=TOKEN, shutdown=shutdown)):
        assert calls == []
    assert calls == ["down"]


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
@pytest.mark.parametrize("missing", ["JARVIS_ADMIN_TOKEN", "JARVIS_MASTER_KEY"])
def test_env_app_stops_startup_when_a_secret_is_missing(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(missing)
    with pytest.raises(SettingsError, match=missing):
        create_app_from_env()


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_env_app_wires_services_and_disposes_them(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    disposed: list[str] = []
    real_dispose = AsyncEngine.dispose
    real_aclose = RedisConfigPublisher.aclose

    async def dispose(self: AsyncEngine, close: bool = True) -> None:
        disposed.append("engine")
        await real_dispose(self, close)

    async def aclose(self: RedisConfigPublisher) -> None:
        disposed.append("publisher")
        await real_aclose(self)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    monkeypatch.setattr(RedisConfigPublisher, "aclose", aclose)

    async def no_migrate(url: str) -> None:
        return None

    monkeypatch.setattr(app_module, "run_migrations", no_migrate)
    app = create_app_from_env()
    with TestClient(app) as client:
        assert client.get("/api/v1/projects").status_code == 401
    assert sorted(disposed) == ["engine", "publisher"]


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_other_http_errors_keep_their_status_in_the_error_format() -> None:
    from fastapi import HTTPException

    app = create_app(services=_services(), admin_token=TOKEN)

    @app.get("/teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=418, detail="short and stout")

    response = TestClient(app).get("/teapot")
    assert response.status_code == 418
    assert set(response.json()["error"]) == {"code", "message"}


@pytest.mark.unit
@pytest.mark.req("REQ-10-2")
@pytest.mark.parametrize("method", ["get", "post", "delete"])
def test_unknown_paths_under_v1_still_need_the_token(client: TestClient, method: str) -> None:
    response = client.request(method, "/api/v1/does/not/exist")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "JV_AUTH_REQUIRED"


@pytest.mark.unit
@pytest.mark.req("REQ-10-2")
def test_wrong_method_on_a_known_path_needs_the_token(client: TestClient) -> None:
    assert client.patch("/api/v1/projects").status_code == 401


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_405_carries_the_allow_header(client: TestClient) -> None:
    response = client.patch("/api/v1/projects", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 405
    # Starlette names the methods of the first route matching the path (here GET only).
    assert "GET" in response.headers["allow"].replace(" ", "").split(",")
    assert response.json()["error"]["code"] == "JV_NOT_IMPLEMENTED"


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_other_http_errors_use_the_detail_as_the_message() -> None:
    from fastapi import HTTPException

    app = create_app(services=_services(), admin_token=TOKEN)

    @app.get("/teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=418, detail="short and stout")

    @app.get("/opaque")
    async def opaque() -> None:
        raise HTTPException(status_code=418, detail={"x": 1})

    client = TestClient(app)
    assert client.get("/teapot").json()["error"]["message"] == "short and stout"
    assert client.get("/opaque").json()["error"]["message"] == "I'm a Teapot"


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "JV_AUTH_REQUIRED"), (409, "JV_CONFLICT"), (501, "JV_NOT_IMPLEMENTED")],
)
def test_http_errors_with_a_matching_jarvis_code_use_it(status: int, code: str) -> None:
    from fastapi import HTTPException

    app = create_app(services=_services(), admin_token=TOKEN)

    @app.get("/raise")
    async def raise_it() -> None:
        raise HTTPException(status_code=status, detail="nope")

    response = TestClient(app).get("/raise")
    assert response.status_code == status
    assert response.json()["error"] == {"code": code, "message": "nope"}


@pytest.mark.unit
@pytest.mark.req("REQ-10-1")
def test_unexpected_exceptions_return_a_bare_500(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(services=_services(), admin_token=TOKEN)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("SENTINEL-DETAIL postgres://u:p@host/db")

    response = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 500
    assert response.json() == {"error": {"code": "JV_INTERNAL", "message": "internal error"}}
    assert "SENTINEL-DETAIL" not in response.text
