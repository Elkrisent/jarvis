"""Shared fixtures for stack-level tests (IMPLEMENTATION §B.6)."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
PROJECT = "jarvis-test"
PORTS = {"gateway": 18080, "api": 18081, "worker": 18082}


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = [
        "docker",
        "compose",
        "-p",
        PROJECT,
        "-f",
        str(DEPLOY / "docker-compose.yml"),
        "-f",
        str(DEPLOY / "docker-compose.test.yml"),
        "--env-file",
        str(DEPLOY / ".env.example"),
        *args,
    ]
    return subprocess.run(cmd, check=check, capture_output=True, text=True, cwd=REPO)


def wait_until[T](
    probe: Callable[[], T | None],
    *,
    timeout_s: float,
    interval_s: float = 0.5,
    what: str = "condition",
) -> T:
    """Poll ``probe`` until it returns something other than None/False, or fail."""
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result is not None and result is not False:
                return result
        except (httpx.HTTPError, subprocess.CalledProcessError) as exc:
            last_error = exc
        time.sleep(interval_s)
    raise AssertionError(
        f"timed out after {timeout_s}s waiting for {what} (last error: {last_error})"
    )


def readyz(service: str) -> httpx.Response:
    return httpx.get(f"http://127.0.0.1:{PORTS[service]}/readyz", timeout=3)


@pytest.fixture(scope="session")
def compose_stack() -> Iterator[None]:
    """Bring up the default profile once per session; tear it down afterwards."""
    # CI prebuilds the images with a layer cache and sets JARVIS_SKIP_BUILD.
    build = [] if os.environ.get("JARVIS_SKIP_BUILD") else ["--build"]
    compose("up", "-d", *build, "--wait", "--wait-timeout", "180")
    try:
        yield
    finally:
        if not os.environ.get("JARVIS_KEEP_STACK"):
            compose("down", "-v", "--remove-orphans", check=False)


@pytest.fixture
def kill() -> Callable[[str], None]:
    """``kill(service)``; the service is always restarted at teardown (see ``revive``)."""
    stopped: list[str] = []

    def _kill(service: str) -> None:
        compose("stop", service)
        stopped.append(service)

    _kill.stopped = stopped  # type: ignore[attr-defined]
    return _kill


@pytest.fixture
def revive(kill: Callable[[str], None]) -> Iterator[Callable[[str], None]]:
    def _revive(service: str) -> None:
        compose("start", service)

    yield _revive
    for service in kill.stopped:  # type: ignore[attr-defined]
        compose("start", service, check=False)
