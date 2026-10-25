#!/usr/bin/env python3
"""Create (and remove) a project, API and key for a load scenario on the compose test stack.

  scenario_setup.py setup --slug s4 --rules '[{"path_prefix": "/"}]' [--api '{"deadline_ms": 5000}']
      -> prints the JARVIS key; resets the mock upstream and installs the given rules
  scenario_setup.py teardown --slug s4

Environment: API_URL (default http://127.0.0.1:18081), ADMIN_TOKEN (default: the placeholder of
deploy/docker-compose.test.yml), MOCK_URL (default http://127.0.0.1:19000), GATEWAY_URL (default
http://127.0.0.1:18080), BENCH_DIR. The project id is kept in $BENCH_DIR/<slug>_project.txt.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from pathlib import Path

import httpx

API_URL = os.environ.get("API_URL", "http://127.0.0.1:18081")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "test-admin-token-placeholder-0000000000")
MOCK_URL = os.environ.get("MOCK_URL", "http://127.0.0.1:19000")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:18080")


def _state(slug: str) -> Path:
    return Path(os.environ.get("BENCH_DIR", "build/bench")) / f"{slug}_project.txt"


def setup(slug: str, rules: list[dict[str, object]], api_extra: dict[str, object]) -> str:
    with httpx.Client(
        base_url=f"{API_URL}/api/v1", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, timeout=10
    ) as api:
        project = api.post("/projects", json={"name": f"{slug} bench", "slug": f"{slug}-bench"})
        project.raise_for_status()
        pid = project.json()["id"]
        _state(slug).parent.mkdir(parents=True, exist_ok=True)
        _state(slug).write_text(pid)
        body = {
            "slug": slug,
            "name": slug.upper(),
            "base_url": "http://mock-upstream:8080",
            "jarvis_key_slot": "authorization",
            "credential": {"mode": "bearer", "secret": secrets.token_urlsafe(24)},  # throwaway
            "deadline_ms": 10000,
            "attempt_timeout_ms": 5000,
            **api_extra,
        }
        api.post(f"/projects/{pid}/apis", json=body).raise_for_status()
        key = api.post(f"/projects/{pid}/keys", json={"name": slug})
        key.raise_for_status()
        jarvis_key: str = key.json()["key"]
    httpx.post(f"{MOCK_URL}/__admin/reset", params={"seed": 0}, timeout=5).raise_for_status()
    httpx.post(f"{MOCK_URL}/__admin/rules", json=rules, timeout=5).raise_for_status()
    deadline = time.monotonic() + 15  # the gateway learns the new API through Redis within ~1 s
    while time.monotonic() < deadline:
        ready = httpx.get(
            f"{GATEWAY_URL}/gw/{slug}/ready", headers={"X-Jarvis-Key": jarvis_key}, timeout=5
        )
        if ready.headers.get("X-Jarvis-Source") and ready.status_code != 401:
            # the gateway knows the API; the probe call itself must not count, so start clean
            httpx.post(f"{MOCK_URL}/__admin/reset", params={"seed": 0}, timeout=5)
            httpx.post(f"{MOCK_URL}/__admin/rules", json=rules, timeout=5).raise_for_status()
            return jarvis_key
        time.sleep(0.2)
    raise SystemExit(f"the gateway never served the {slug} API")


def teardown(slug: str) -> None:
    state = _state(slug)
    if state.exists():
        with httpx.Client(
            base_url=f"{API_URL}/api/v1",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            timeout=10,
        ) as api:
            api.delete(f"/projects/{state.read_text().strip()}")
        state.unlink()
    httpx.post(f"{MOCK_URL}/__admin/reset", params={"seed": 0}, timeout=5)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["setup", "teardown"])
    ap.add_argument("--slug", required=True)
    ap.add_argument("--rules", default='[{"path_prefix": "/"}]')
    ap.add_argument("--api", default="{}")
    args = ap.parse_args()
    if args.command == "setup":
        print(setup(args.slug, json.loads(args.rules), json.loads(args.api)))
    else:
        teardown(args.slug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
