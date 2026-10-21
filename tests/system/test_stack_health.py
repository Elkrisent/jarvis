import json
import subprocess

import pytest

from tests.conftest import PORTS, readyz, wait_until

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
