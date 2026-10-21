import json
import subprocess

import pytest

from tests.conftest import compose

SERVICES = ["gateway", "api", "worker"]

pytestmark = [pytest.mark.security, pytest.mark.usefixtures("compose_stack")]


@pytest.mark.req("REQ-11.3-2")
@pytest.mark.parametrize("service", SERVICES)
def test_image_is_configured_to_run_as_non_root(service: str) -> None:
    out = subprocess.run(
        ["docker", "image", "inspect", f"jarvis-{service}:dev"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    user = json.loads(out)[0]["Config"]["User"]
    assert user not in ("", "0", "root", "0:0", "root:root")
    assert user.split(":")[0].isdigit()
    assert int(user.split(":")[0]) > 0


@pytest.mark.req("REQ-11.3-2")
@pytest.mark.parametrize("service", SERVICES)
def test_running_container_is_not_uid_0(service: str) -> None:
    uid = compose("exec", "-T", service, "id", "-u").stdout.strip()
    assert uid != "0"


@pytest.mark.parametrize("service", SERVICES)
def test_root_filesystem_is_read_only(service: str) -> None:
    result = compose("exec", "-T", service, "sh", "-c", "touch /should-fail", check=False)
    assert result.returncode != 0


@pytest.mark.req("REQ-2-2", "REQ-11.3-2")
def test_default_compose_publishes_no_host_ports() -> None:
    out = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "deploy/docker-compose.yml",
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
    services = json.loads(out)["services"]
    assert {name for name, svc in services.items() if svc.get("ports")} == set()
