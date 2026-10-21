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
MOCK_UPSTREAM_URL = "http://127.0.0.1:19000"


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = [
        "docker",
        "compose",
        "-p",
        PROJECT,
        "--profile",
        "test",
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


class MockUpstream:
    """Client for the mock upstream admin API (see tools/mock_upstream/README.md)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._http = httpx.Client(base_url=base_url, timeout=5)

    def reset(self, seed: int | None = None) -> None:
        self._http.post(
            "/__admin/reset", params={"seed": seed} if seed is not None else {}
        ).raise_for_status()

    def set_rules(self, *rules: dict[str, object]) -> None:
        self._http.post("/__admin/rules", json=list(rules)).raise_for_status()

    def calls(self, path_prefix: str = "") -> list[dict[str, object]]:
        r = self._http.get("/__admin/calls", params={"path_prefix": path_prefix})
        r.raise_for_status()
        return list(r.json()["calls"])

    def close(self) -> None:
        self._http.close()


@pytest.fixture
def mock_upstream(compose_stack: None) -> Iterator[MockUpstream]:
    """A reset mock upstream, controllable from the test."""
    mock = MockUpstream(MOCK_UPSTREAM_URL)
    mock.reset(seed=0)
    yield mock
    mock.reset()
    mock.close()


def github_repo() -> str:
    """``owner/name`` of this repository, from the Actions env or the origin remote."""
    if repo := os.environ.get("GITHUB_REPOSITORY"):
        return repo
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO,
    ).stdout.strip()
    return url.removesuffix(".git").split("github.com")[-1].lstrip(":/")


def github_headers() -> dict[str, str]:
    """GitHub API headers, authenticated when a token is available (Actions or `gh`)."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        gh = subprocess.run(
            ["gh", "auth", "token"],
            check=False,
            capture_output=True,
            text=True,
        )
        token = gh.stdout.strip() if gh.returncode == 0 else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
