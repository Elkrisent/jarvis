from collections.abc import Callable

import pytest

from tests.conftest import readyz, wait_until

pytestmark = [pytest.mark.chaos, pytest.mark.usefixtures("compose_stack")]


def _status(service: str) -> tuple[int, str]:
    r = readyz(service)
    return r.status_code, r.json()["status"]


def _wait_for(service: str, expected: tuple[int, str]) -> None:
    wait_until(
        lambda: _status(service) == expected or None,
        timeout_s=45,
        what=f"{service} /readyz == {expected}",
    )


def test_redis_outage_degrades_then_recovers(
    kill: Callable[[str], None], revive: Callable[[str], None]
) -> None:
    for service in ("gateway", "api", "worker"):
        _wait_for(service, (200, "ok"))

    kill("redis")

    _wait_for("gateway", (200, "degraded"))  # fails open (D3)
    _wait_for("api", (200, "degraded"))
    _wait_for("worker", (503, "unavailable"))  # cannot consume the stream without Redis

    revive("redis")

    for service in ("gateway", "api", "worker"):
        _wait_for(service, (200, "ok"))
