from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from jarvis_core.config import ApiDependency
from jarvis_core.keys import hash_jarvis_key
from jarvis_gateway.app import create_app
from jarvis_gateway.config.manager import ConfigManager
from jarvis_gateway.config.snapshot import ApiEntry, KeyEntry, Snapshot
from jarvis_gateway.config.store import ConfigStore

pytestmark = pytest.mark.unit

KEY = "jv_live_goodkey000"
REVOKED = "jv_live_revoked000"
OTHER_PROJECT = "jv_live_otherproj0"
RESTRICTED = "jv_live_restricted0"


class StoreOnly:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


def _api(api_id: str, project: str, slug: str) -> ApiEntry:
    raw: dict[str, Any] = {
        "slug": slug,
        "name": slug,
        "base_url": "https://api.example.com",
        "jarvis_key_slot": "authorization",
    }
    return ApiEntry(api_id, project, slug, 1, ApiDependency.model_validate(raw))


def _client(loaded: bool = True) -> TestClient:
    store = ConfigStore()
    if loaded:
        keys = {
            hash_jarvis_key(KEY): KeyEntry("k1", "p1", frozenset(), False),
            hash_jarvis_key(REVOKED): KeyEntry("k2", "p1", frozenset(), True),
            hash_jarvis_key(OTHER_PROJECT): KeyEntry("k3", "p2", frozenset(), False),
            hash_jarvis_key(RESTRICTED): KeyEntry("k4", "p1", frozenset({"a2"}), False),
        }
        apis = [_api("a1", "p1", "weather"), _api("a2", "p1", "maps")]
        store.replace(
            Snapshot.empty("postgres", 1.0).with_changes(
                apis={a.api_id: a for a in apis}, keys=keys
            )
        )
    manager = cast(ConfigManager, StoreOnly(store))
    return TestClient(create_app(config=manager))


def _is_ulid(value: str) -> bool:
    return len(value) == 26 and value.isalnum()


def test_a_valid_key_is_authenticated_but_not_forwarded_yet() -> None:
    response = _client().get("/gw/weather/v1/current?city=x", headers={"X-Jarvis-Key": KEY})
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "JV_NOT_IMPLEMENTED"
    assert response.headers["x-jarvis-source"] == "jarvis"
    assert response.headers["x-jarvis-request-id"] == response.json()["error"]["request_id"]


def test_the_slot_key_authenticates_too() -> None:
    response = _client().get("/gw/weather", headers={"Authorization": f"Bearer {KEY}"})
    assert response.status_code == 501


@pytest.mark.req("REQ-4.2-4")
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Jarvis-Key": "jv_live_unknown000"},
        {"X-Jarvis-Key": REVOKED},
        {"Authorization": f"Bearer {REVOKED}"},
        {"X-Jarvis-Key": "x" * 300_000},
    ],
)
def test_missing_unknown_revoked_and_oversized_keys_get_401(headers: dict[str, str]) -> None:
    response = _client().get("/gw/weather/x", headers=headers)
    body = response.json()["error"]
    assert response.status_code == 401
    assert (body["code"], body["message"]) == ("JV_AUTH_INVALID", "invalid or missing JARVIS key")
    assert _is_ulid(body["request_id"])
    assert response.headers["x-jarvis-request-id"] == body["request_id"]
    assert response.headers["x-jarvis-source"] == "jarvis"


@pytest.mark.req("REQ-4.2-4")
@pytest.mark.req("REQ-4.2-5")
def test_wrong_project_and_disallowed_api_get_the_same_401() -> None:
    client = _client()
    wrong_project = client.get("/gw/weather", headers={"X-Jarvis-Key": OTHER_PROJECT})
    disallowed = client.get("/gw/weather", headers={"X-Jarvis-Key": RESTRICTED})
    allowed = client.get("/gw/maps", headers={"X-Jarvis-Key": RESTRICTED})
    assert wrong_project.status_code == disallowed.status_code == 401
    assert allowed.status_code == 501
    for response in (wrong_project, disallowed):
        error = dict(response.json()["error"], request_id="-")
        assert error == {
            "code": "JV_AUTH_INVALID",
            "message": "invalid or missing JARVIS key",
            "request_id": "-",
        }


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_every_method_authenticates_the_same(method: str) -> None:
    client = _client()
    good = client.request(method, "/gw/weather/a/b", headers={"X-Jarvis-Key": KEY}, content=b"x")
    bad = client.request(method, "/gw/weather/a/b", content=b"x")
    assert (good.status_code, bad.status_code) == (501, 401)


def test_before_the_first_config_load_the_route_answers_503() -> None:
    response = _client(loaded=False).get("/gw/weather", headers={"X-Jarvis-Key": KEY})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "JV_NOT_READY"


def test_health_endpoints_need_no_key() -> None:
    assert _client().get("/healthz").status_code == 200
