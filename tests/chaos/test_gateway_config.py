"""Gateway config under Postgres loss and snapshot-file startup (M1.4; SPEC §8)."""

import json
import subprocess
from collections.abc import Callable

import httpx
import pytest

from tests.conftest import (
    ADMIN_TOKEN,
    API_URL,
    PROJECT,
    compose,
    create_project_with_api,
    readyz,
    wait_until,
)

pytestmark = [pytest.mark.chaos, pytest.mark.usefixtures("compose_stack")]

SNAPSHOT_VOLUME = f"{PROJECT}_gateway-snapshot"
GATEWAY_IMAGE = "jarvis-gateway:dev"


def _state() -> tuple[int, str, dict[str, object]]:
    r = readyz("gateway")
    body = r.json()
    return r.status_code, body["status"], body["config"]


def _wait_config(
    what: str, ok: Callable[[int, str, dict[str, object]], bool], timeout_s: float
) -> None:
    wait_until(
        lambda: ok(*_state()) or None,
        timeout_s=timeout_s,
        what=what,
    )


def _gateway_running() -> bool:
    out = compose("ps", "-q", "gateway").stdout.strip()
    if not out:
        return False
    state = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", out],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return state == "true"


def _clear_snapshot_volume() -> None:
    subprocess.run(
        [
            "docker", "run", "--rm", "--user", "0", "-v", f"{SNAPSHOT_VOLUME}:/v",
            "--entrypoint", "sh", GATEWAY_IMAGE, "-c", "rm -f /v/snapshot.json /v/.snapshot-*",
        ],
        check=True,
        capture_output=True,
        text=True,
    )  # fmt: skip


@pytest.mark.req("REQ-8-2", "REQ-8-8", "REQ-8-9")
def test_postgres_outage_snapshot_start_and_recovery(
    kill: Callable[[str], None], revive: Callable[[str], None]
) -> None:
    _wait_config(
        "gateway postgres+ok",
        lambda c, s, cfg: (c, s, cfg["source"]) == (200, "ok", "postgres"),
        60,
    )
    with httpx.Client(
        base_url=f"{API_URL}/api/v1",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        timeout=10,
    ) as client:
        pid, _, _ = create_project_with_api(client, "chaos-config")
        try:
            wait_until(
                lambda: _state()[2]["apis"] or None, timeout_s=10, what="API visible in config"
            )
            # Let the snapshot writer's one-per-second throttle flush the latest config.
            before = _state()[2]
            apis, keys = before["apis"], before["keys"]

            # 1. Postgres goes away: the running gateway keeps serving the same config.
            kill("postgres")
            _wait_config(
                "stale-but-serving",
                lambda c, s, cfg: c == 200
                and cfg["source"] == "postgres"
                and (cfg["apis"], cfg["keys"]) == (apis, keys)
                and s == "degraded",
                30,
            )

            # 2. A restart with Postgres down starts from the snapshot file.
            compose("restart", "gateway")
            _wait_config(
                "file-backed",
                lambda c, s, cfg: (c, s, cfg["source"]) == (200, "degraded", "file")
                and (cfg["apis"], cfg["keys"]) == (apis, keys),
                60,
            )

            # 3. Postgres returns: the next resync (about the interval) restores the source.
            revive("postgres")
            _wait_config(
                "postgres again",
                lambda c, s, cfg: (c, s, cfg["source"]) == (200, "ok", "postgres"),
                60,
            )

            # 4. No snapshot file and no Postgres: the gateway stays up and reports 503.
            kill("postgres")
            compose("stop", "gateway")
            _clear_snapshot_volume()
            # `compose start gateway` would also start its dependency (Postgres): start the
            # container directly so Postgres stays down.
            container = compose("ps", "-q", "-a", "gateway").stdout.strip()
            subprocess.run(["docker", "start", container], check=True, capture_output=True)
            _wait_config(
                "unavailable without any config",
                lambda c, s, cfg: (c, s, cfg["source"], cfg["apis"])
                == (503, "unavailable", "none", 0),
                60,
            )
            assert _gateway_running()
            assert httpx.get("http://127.0.0.1:18080/healthz", timeout=3).status_code == 200

            # 5. Postgres returns: the 5 s retry loads the config.
            revive("postgres")
            _wait_config(
                "ready after Postgres returns",
                lambda c, s, cfg: (c, s, cfg["source"]) == (200, "ok", "postgres")
                and cfg["apis"] == apis,
                60,
            )
        finally:
            compose("start", "postgres", check=False)
            wait_until(
                lambda: (client.delete(f"/projects/{pid}").status_code in (204, 404) or None),
                timeout_s=60,
                what="cleanup",
            )
    assert json.loads(readyz("gateway").text)["config"]["source"] in ("postgres", "file")
