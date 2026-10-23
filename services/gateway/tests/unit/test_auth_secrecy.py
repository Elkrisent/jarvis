import logging
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

GOOD = "jv_live_SECRETgood0001"
REVOKED = "jv_live_SECRETrevoked1"
UNKNOWN = "jv_live_SECRETunknown1"


class StoreOnly:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store


def _client() -> TestClient:
    raw: dict[str, Any] = {
        "slug": "weather",
        "name": "w",
        "base_url": "https://api.example.com",
        "jarvis_key_slot": "authorization",
    }
    api = ApiEntry("a1", "p1", "weather", 1, ApiDependency.model_validate(raw))
    keys = {
        hash_jarvis_key(GOOD): KeyEntry("k1", "p1", frozenset(), False),
        hash_jarvis_key(REVOKED): KeyEntry("k2", "p1", frozenset(), True),
    }
    store = ConfigStore()
    store.replace(Snapshot.empty("postgres", 1.0).with_changes(apis={"a1": api}, keys=keys))
    return TestClient(create_app(config=cast(ConfigManager, StoreOnly(store))))


@pytest.mark.req("REQ-4.2-3")
def test_the_key_never_reaches_a_log_record_or_a_response_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client()
    secrets = [GOOD, REVOKED, UNKNOWN]
    hashes = [hash_jarvis_key(s) for s in secrets]
    bodies: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for key in secrets:
            for headers in ({"X-Jarvis-Key": key}, {"Authorization": f"Bearer {key}"}):
                bodies.append(client.get("/gw/weather/x?token=1", headers=headers).text)
        bodies.append(client.get("/gw/weather/x", headers={"X-Jarvis-Key": "x" * 5000}).text)
    rendered = "\n".join(
        [r.getMessage() + repr(r.args) + str(r.exc_info) for r in caplog.records] + bodies
    )
    for needle in [*secrets, *hashes, "SECRET"]:
        assert needle not in rendered
