import json
import subprocess
import time
from collections.abc import Callable

import httpx
import pytest

from tests.conftest import (
    ADMIN_TOKEN,
    API_URL,
    PORTS,
    create_project_with_api,
    gateway_config,
    readyz,
    wait_until,
)

pytestmark = [pytest.mark.system, pytest.mark.usefixtures("compose_stack")]


@pytest.mark.req("REQ-15.1-2")
@pytest.mark.parametrize("service", sorted(PORTS))
def test_readyz_returns_200_ok_within_60s(service: str) -> None:
    response = wait_until(
        lambda: r if (r := readyz(service)).status_code == 200 else None,
        timeout_s=60,
        what=f"{service} /readyz",
    )
    assert response.json()["status"] == "ok"


@pytest.mark.req("REQ-15.1-4")
def test_dev_override_builds_from_source_with_hot_reload() -> None:
    out = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "deploy/docker-compose.yml",
            "-f",
            "deploy/docker-compose.dev.yml",
            "--env-file",
            "deploy/.env.example",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for service in ("gateway", "api", "worker"):
        svc = json.loads(out)["services"][service]
        assert "--reload" in " ".join(svc["command"])
        assert "build" in svc


PROPAGATION_BUDGET_S = 1.0


def _seen_within_budget(
    predicate_what: str, predicate: Callable[[dict[str, object]], bool], started: float
) -> None:
    """Poll the gateway's /readyz until ``predicate(config)`` holds; assert it took <= 1 s."""
    wait_until(
        lambda: predicate(gateway_config()) or None,
        timeout_s=PROPAGATION_BUDGET_S + 5,
        interval_s=0.05,
        what=predicate_what,
    )
    elapsed = time.monotonic() - started
    assert elapsed <= PROPAGATION_BUDGET_S, f"{predicate_what} took {elapsed:.2f}s"


@pytest.mark.req("REQ-5.2-3", "REQ-13-5", "REQ-4.2-6")
def test_gateway_loads_config_from_postgres_and_follows_changes_within_1s() -> None:
    wait_until(
        lambda: gateway_config()["source"] == "postgres" or None,
        timeout_s=30,
        what="gateway config source == postgres",
    )
    before = gateway_config()
    base_apis, base_keys = int(str(before["apis"])), int(str(before["keys"]))
    assert isinstance(before["synced_at"], float)

    with httpx.Client(
        base_url=f"{API_URL}/api/v1",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        timeout=10,
    ) as client:
        project = client.post("/projects", json={"name": "Prop", "slug": "propagation-test"})
        assert project.status_code == 201, project.text
        pid = project.json()["id"]

        started = time.monotonic()
        api = client.post(
            f"/projects/{pid}/apis",
            json={
                "slug": "mock",
                "name": "Mock",
                "base_url": "http://mock-upstream:8080",
                "credential": {"mode": "bearer", "secret": "sk-stack-test-placeholder"},
            },
        )
        assert api.status_code == 201, api.text
        _seen_within_budget("apis count up", lambda c: c["apis"] == base_apis + 1, started)

        started = time.monotonic()
        key = client.post(f"/projects/{pid}/keys", json={"name": "ci"})
        assert key.status_code == 201, key.text
        _seen_within_budget("keys count up", lambda c: c["keys"] == base_keys + 1, started)

        # A revoked key stays in the gateway's config, flagged revoked (the count is unchanged).
        assert client.delete(f"/keys/{key.json()['id']}").status_code == 204
        time.sleep(PROPAGATION_BUDGET_S)  # settle: nothing further should change
        assert gateway_config()["keys"] == base_keys + 1

        started = time.monotonic()
        assert client.delete(f"/projects/{pid}").status_code == 204
        _seen_within_budget(
            "project's apis and keys gone",
            lambda c: c["apis"] == base_apis and c["keys"] == base_keys,
            started,
        )


@pytest.mark.req("REQ-5.2-3")
def test_gateway_readyz_config_reports_counts_only() -> None:
    with httpx.Client(
        base_url=f"{API_URL}/api/v1",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        timeout=10,
    ) as client:
        pid, _, _ = create_project_with_api(client, "counts-only")
        try:
            wait_until(
                lambda: gateway_config()["apis"] or None, timeout_s=10, what="an API in config"
            )
            body = readyz("gateway").text
            assert "counts-only" not in body
            assert "sk-stack-test-placeholder" not in body
            assert set(json.loads(body)["config"]) == {"source", "apis", "keys", "synced_at"}
        finally:
            client.delete(f"/projects/{pid}")
