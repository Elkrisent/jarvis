import asyncio

import pytest
from jarvis_core.health import (
    Dependency,
    dependency_from_url,
    evaluate_readiness,
    tcp_reachable,
)

pytestmark = pytest.mark.unit

REDIS_SOFT = Dependency("redis", "redis", 6379, required=False)
PG_HARD = Dependency("postgres", "pg", 5432, required=True)


def _probe(down: set[str]):  # type: ignore[no-untyped-def]
    async def probe(host: str, port: int) -> bool:
        return host not in down

    return probe


async def test_all_up_is_ok() -> None:
    r = await evaluate_readiness([REDIS_SOFT, PG_HARD], _probe(set()))
    assert (r.status, r.http_status) == ("ok", 200)
    assert r.checks == {"redis": True, "postgres": True}


async def test_optional_dependency_down_is_degraded_but_still_200() -> None:
    r = await evaluate_readiness([REDIS_SOFT, PG_HARD], _probe({"redis"}))
    assert (r.status, r.http_status) == ("degraded", 200)
    assert r.checks["redis"] is False


async def test_required_dependency_down_is_unavailable_503() -> None:
    r = await evaluate_readiness([REDIS_SOFT, PG_HARD], _probe({"pg"}))
    assert (r.status, r.http_status) == ("unavailable", 503)


async def test_no_dependencies_is_ok() -> None:
    assert (await evaluate_readiness([], _probe(set()))).status == "ok"


async def test_body_shape() -> None:
    r = await evaluate_readiness([PG_HARD], _probe(set()))
    assert r.to_body() == {"status": "ok", "checks": {"postgres": True}}


def test_dependency_from_url() -> None:
    dep = dependency_from_url("db", "postgresql://u:p@postgres:5432/jarvis", required=False)
    assert dep == Dependency("db", "postgres", 5432, False)


@pytest.mark.parametrize("url", ["redis://redis", "nonsense"])
def test_dependency_url_needs_host_and_port(url: str) -> None:
    with pytest.raises(ValueError, match="host and port"):
        dependency_from_url("x", url)


async def test_tcp_reachable_against_a_real_local_listener() -> None:
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await tcp_reachable("127.0.0.1", port) is True
    finally:
        server.close()
        await server.wait_closed()
    assert await tcp_reachable("127.0.0.1", port, timeout_s=0.5) is False
