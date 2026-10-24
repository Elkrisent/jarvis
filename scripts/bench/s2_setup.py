#!/usr/bin/env python3
"""Create (and remove) what the S2 pass-through scenario needs on the compose test stack.

  s2_setup.py setup     -> prints the JARVIS key; fixes the mock upstream latency
  s2_setup.py teardown  -> deletes the project and clears the mock upstream rules

Environment: API_URL (default http://127.0.0.1:18081), ADMIN_TOKEN (default: the placeholder of
deploy/docker-compose.test.yml), MOCK_URL (default http://127.0.0.1:19000), S2_UPSTREAM_MS (50).
The project id is kept in build/bench/s2_project.txt between the two calls.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

import httpx

API_URL = os.environ.get("API_URL", "http://127.0.0.1:18081")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "test-admin-token-placeholder-0000000000")
MOCK_URL = os.environ.get("MOCK_URL", "http://127.0.0.1:19000")
UPSTREAM_MS = int(os.environ.get("S2_UPSTREAM_MS", "50"))
STATE = Path(os.environ.get("BENCH_DIR", "build/bench")) / "s2_project.txt"
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:18080")


def setup() -> str:
    with httpx.Client(
        base_url=f"{API_URL}/api/v1", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, timeout=10
    ) as api:
        project = api.post("/projects", json={"name": "S2 bench", "slug": "s2-bench"})
        project.raise_for_status()
        pid = project.json()["id"]
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(pid)
        api.post(
            f"/projects/{pid}/apis",
            json={
                "slug": "s2",
                "name": "S2",
                "base_url": "http://mock-upstream:8080",
                "jarvis_key_slot": "authorization",
                "credential": {"mode": "bearer", "secret": secrets.token_urlsafe(24)},  # throwaway
                "deadline_ms": 10000,
                "attempt_timeout_ms": 5000,
            },
        ).raise_for_status()
        key = api.post(f"/projects/{pid}/keys", json={"name": "s2"})
        key.raise_for_status()
        jarvis_key: str = key.json()["key"]
    httpx.post(f"{MOCK_URL}/__admin/reset", params={"seed": 0}, timeout=5).raise_for_status()
    httpx.post(
        f"{MOCK_URL}/__admin/rules",
        json=[{"path_prefix": "/", "latency_ms": UPSTREAM_MS}],
        timeout=5,
    ).raise_for_status()
    deadline = time.monotonic() + 15  # the gateway learns the new API through Redis within ~1 s
    while time.monotonic() < deadline:
        ready = httpx.get(
            f"{GATEWAY_URL}/gw/s2/ready", headers={"X-Jarvis-Key": jarvis_key}, timeout=5
        )
        if ready.status_code == 200:
            return jarvis_key
        time.sleep(0.2)
    raise SystemExit("the gateway never served the S2 API")


def teardown() -> None:
    if STATE.exists():
        with httpx.Client(
            base_url=f"{API_URL}/api/v1",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            timeout=10,
        ) as api:
            api.delete(f"/projects/{STATE.read_text().strip()}")
        STATE.unlink()
    httpx.post(f"{MOCK_URL}/__admin/reset", params={"seed": 0}, timeout=5)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "setup":
        print(setup())
    elif command == "teardown":
        teardown()
    else:
        raise SystemExit(__doc__)
