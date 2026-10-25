"""Events land in the Redis Stream with the exact schema (M1.9; SPEC §6.1)."""

import json
from pathlib import Path

import httpx
import pytest

from tests.conftest import ADMIN_TOKEN, API_URL, PORTS, MockUpstream, telemetry_events, wait_until

pytestmark = [pytest.mark.system, pytest.mark.usefixtures("compose_stack")]

GATEWAY = f"http://127.0.0.1:{PORTS['gateway']}"
SNAPSHOT = json.loads(
    (Path(__file__).resolve().parents[1] / "contract" / "telemetry.snapshot.json").read_text()
)
SECRET = "sk-telemetry-story-secret"


@pytest.mark.req("REQ-6.1-1", "REQ-6.1-2", "REQ-3-2")
def test_proxied_requests_become_events_with_the_exact_schema(
    mock_upstream: MockUpstream,
) -> None:
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    with httpx.Client(base_url=f"{API_URL}/api/v1", headers=admin, timeout=10) as api:
        project = api.post("/projects", json={"name": "Telemetry", "slug": "telemetry-story"})
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
                    "credential": {"mode": "bearer", "secret": SECRET},
                },
            )
            assert created.status_code == 201, created.text
            key = api.post(f"/projects/{pid}/keys", json={"name": "app"})
            assert key.status_code == 201, key.text
            headers = {"X-Jarvis-Key": key.json()["key"]}

            def call(path: str) -> httpx.Response:
                return httpx.get(f"{GATEWAY}/gw/weather{path}", headers=headers, timeout=10)

            wait_until(
                lambda: call("/v1/ready").status_code == 200 or None,
                timeout_s=10,
                interval_s=0.1,
                what="the gateway to know the new API",
            )
            responses = [call(f"/v1/users/{n}?token=QUERY-SECRET") for n in range(1, 21)]
            ids = {r.headers["x-jarvis-request-id"] for r in responses}
            assert len(ids) == 20

            def mine() -> list[dict[str, object]] | None:
                found = [e for e in telemetry_events() if e.get("request_id") in ids]
                return found if len(found) == 20 else None

            events = wait_until(mine, timeout_s=15, interval_s=0.3, what="20 telemetry events")
        finally:
            api.delete(f"/projects/{pid}")

    field_names = [name for name, _ in SNAPSHOT["fields"]]
    for event in events:
        assert list(event) == field_names
        assert event["v"] == SNAPSHOT["v"] == 1
        assert event["kind"] == "request"
        assert event["project_id"] == pid
        assert event["api_id"] == created.json()["id"]
        assert event["key_id"] == key.json()["id"]
        assert event["method"] == "GET"
        assert event["route"] == "/v1/users/{id}"
        assert event["status"] == 200
        assert event["source"] == "upstream"
        assert event["attempts"] == 1
    blob = json.dumps(events)
    assert SECRET not in blob
    assert "QUERY-SECRET" not in blob
    assert key.json()["key"] not in blob
