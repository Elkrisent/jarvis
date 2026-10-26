"""Checkpoint 2 acceptance stories (IMPLEMENTATION, Phase 2: Reliability).

1. Given an upstream failing 60% of the time, when traffic continues, then the circuit opens,
   requests fail fast, one probe tests recovery, and the circuit closes once the upstream is
   healthy.
2. Given a POST without an idempotency config, when the upstream returns 503, then JARVIS does not
   retry.
3. Given a quota of 60/min, when 200 requests arrive in a minute, then the upstream receives <= 60.
4. Given Redis is killed during load, when traffic continues, then no request fails because of
   JARVIS.
"""

import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from tests.conftest import ADMIN_TOKEN, API_URL, PORTS, MockUpstream, readyz, wait_until

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("compose_stack")]

GATEWAY = f"http://127.0.0.1:{PORTS['gateway']}"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@contextmanager
def project_with_api(slug: str, **api_extra: Any) -> Iterator[tuple[str, httpx.Client]]:
    """(JARVIS key, gateway client) for a mock-upstream API named ``provider``."""
    with httpx.Client(base_url=f"{API_URL}/api/v1", headers=ADMIN, timeout=10) as api:
        project = api.post("/projects", json={"name": slug, "slug": slug})
        assert project.status_code == 201, project.text
        pid = project.json()["id"]
        try:
            created = api.post(
                f"/projects/{pid}/apis",
                json={
                    "slug": "provider",
                    "name": "Provider",
                    "base_url": "http://mock-upstream:8080",
                    "jarvis_key_slot": "authorization",
                    "credential": {"mode": "bearer", "secret": "sk-checkpoint-2-provider-secret"},
                    **api_extra,
                },
            )
            assert created.status_code == 201, created.text
            key = api.post(f"/projects/{pid}/keys", json={"name": "app"})
            assert key.status_code == 201, key.text
            jarvis_key = key.json()["key"]
            with httpx.Client(
                base_url=f"{GATEWAY}/gw/provider",
                headers={"Authorization": f"Bearer {jarvis_key}"},
                timeout=15,
            ) as gateway:
                # The gateway learns the new API through Redis within about a second.
                wait_until(
                    lambda: gateway.get("/ready-probe").status_code != 401 or None,
                    timeout_s=15,
                    interval_s=0.1,
                    what="the gateway to know the API and key",
                )
                yield jarvis_key, gateway
        finally:
            wait_until(
                lambda: api.delete(f"/projects/{pid}").status_code in (204, 404) or None,
                timeout_s=60,
                what="the project to be deleted",
            )


def _error_code(response: httpx.Response) -> str | None:
    if response.headers.get("content-type", "").startswith("application/json"):
        error = response.json().get("error")
        return error.get("code") if isinstance(error, dict) else None
    return None


@pytest.mark.req("REQ-4.8-4", "REQ-4.8-5", "REQ-4.8-6", "REQ-4.8-7", "REQ-4.8-8", "REQ-4.8-10")
def test_story_1_the_circuit_opens_fails_fast_probes_once_and_closes(
    mock_upstream: MockUpstream,
) -> None:
    mock_upstream.set_rules({"path_prefix": "/", "errors": [{"status": 503, "rate": 0.6}]})
    with project_with_api(
        "cp2-story-1",
        retry={"enabled": False},
        circuit={
            "failure_rate_threshold": 0.5,
            "min_calls": 10,
            "window_s": 30,
            "open_duration_s": 2,
            "max_open_duration_s": 8,
        },
    ) as (_, gateway):
        # Traffic continues: the circuit opens once enough calls have failed.
        seen_open = False
        for _ in range(200):
            if _error_code(gateway.get("/v1/ping")) == "JV_CIRCUIT_OPEN":
                seen_open = True
                break
        assert seen_open, "the circuit never opened against a 60% failing upstream"

        # Open: requests fail fast and do not reach the upstream.
        before = len(mock_upstream.calls("/v1/"))
        refused = [gateway.get("/v1/ping") for _ in range(20)]
        assert {_error_code(r) for r in refused} == {"JV_CIRCUIT_OPEN"}
        assert {r.status_code for r in refused} == {503}
        assert {r.headers["x-jarvis-circuit"] for r in refused} == {"OPEN"}
        assert len(mock_upstream.calls("/v1/")) == before

        # After the open period only one probe goes out, even for concurrent requests. The
        # upstream is still failing, so the probe fails and the circuit reopens.
        time.sleep(2.2)
        before = len(mock_upstream.calls("/v1/"))
        mock_upstream.set_rules({"path_prefix": "/", "status": 503})
        results: list[httpx.Response] = []
        lock = threading.Lock()

        def hit() -> None:
            response = gateway.get("/v1/ping")
            with lock:
                results.append(response)

        threads = [threading.Thread(target=hit) for _ in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(mock_upstream.calls("/v1/")) - before == 1, "more than one probe was sent"

        # The upstream recovers; the next probe (after the doubled open period) closes it.
        mock_upstream.set_rules({"path_prefix": "/"})
        time.sleep(4.2)
        probe = gateway.get("/v1/ping")
        assert probe.status_code == 200, (probe.status_code, probe.text)
        assert all(gateway.get("/v1/ping").status_code == 200 for _ in range(10))


@pytest.mark.req("REQ-4.7-3", "REQ-4.7-4")
def test_story_2_a_post_without_an_idempotency_config_is_not_retried(
    mock_upstream: MockUpstream,
) -> None:
    mock_upstream.set_rules({"path_prefix": "/", "status": 503})
    with project_with_api("cp2-story-2", retry={"enabled": True, "max_attempts": 3}) as (_, gw):
        post = gw.post("/v1/charge", json={"amount": 1})
        assert post.status_code == 503
        assert post.headers["x-jarvis-attempts"] == "1"
        assert len(mock_upstream.calls("/v1/charge")) == 1
        # Control: a GET against the same upstream is retried, so the retry path is live.
        # The retry budget (20% of the last minute's requests) needs a few requests in the window.
        attempts = [int(gw.get("/v1/read").headers["x-jarvis-attempts"]) for _ in range(15)]
        assert max(attempts) > 1, attempts


@pytest.mark.req("REQ-4.11-3")
def test_story_3_a_quota_of_60_per_minute_caps_what_the_upstream_receives(
    mock_upstream: MockUpstream,
) -> None:
    mock_upstream.set_rules({"path_prefix": "/"})
    with project_with_api(
        "cp2-story-3",
        rate_limits={"upstream": {"enabled": True, "capacity": 60, "refill_per_s": 1.0}},
    ) as (_, gateway):
        started = time.monotonic()
        statuses = [gateway.get(f"/v1/item-{i}").status_code for i in range(200)]
        elapsed = time.monotonic() - started
        received = len(mock_upstream.calls("/v1/"))
        # A bucket of 60 refilling at 1/s admits 60 plus what the run's duration refills.
        assert received <= 60 + math.ceil(elapsed), (received, elapsed)
        assert received >= 55  # the readiness probe above spent a few tokens
        assert statuses.count(200) == received
        assert set(statuses) <= {200, 429}


@pytest.mark.req("REQ-2-4", "REQ-4.11-4")
def test_story_4_no_request_fails_because_of_jarvis_when_redis_is_killed_during_load(
    mock_upstream: MockUpstream,
    kill: Callable[[str], None],
    revive: Callable[[str], None],
) -> None:
    mock_upstream.set_rules({"path_prefix": "/", "latency_ms": 20})
    with project_with_api(
        "cp2-story-4",
        rate_limits={"inbound": {"enabled": True, "capacity": 100000, "refill_per_s": 100000}},
    ) as (_, gateway):
        statuses: list[int] = []
        stop = threading.Event()

        def load() -> None:
            while not stop.is_set():
                statuses.append(gateway.get("/v1/ping").status_code)
                time.sleep(0.01)

        workers = [threading.Thread(target=load) for _ in range(4)]
        for worker in workers:
            worker.start()
        try:
            time.sleep(1.5)
            kill("redis")
            wait_until(
                lambda: readyz("gateway").json().get("redis", {}).get("mode") == "degraded" or None,
                timeout_s=15,
                interval_s=0.2,
                what="the gateway to report degraded mode",
            )
            time.sleep(2.0)
            revive("redis")
            wait_until(
                lambda: readyz("gateway").json().get("redis", {}).get("mode") == "normal" or None,
                timeout_s=30,
                interval_s=0.2,
                what="the gateway to return to normal mode",
            )
            time.sleep(1.0)
        finally:
            stop.set()
            for worker in workers:
                worker.join()
        assert len(statuses) > 100
        assert set(statuses) == {200}, {s: statuses.count(s) for s in set(statuses)}
