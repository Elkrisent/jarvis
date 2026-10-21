import pytest
from fastapi.testclient import TestClient
from jarvis_api.app import create_app, dependencies_from_env
from jarvis_core.health import Dependency

pytestmark = pytest.mark.unit

ENV = {"REDIS_URL": "redis://redis:6379/0", "DATABASE_URL": "postgresql://u:p@postgres:5432/jarvis"}


def _client(down: set[str]) -> TestClient:
    async def probe(host: str, port: int) -> bool:
        return host not in down

    return TestClient(create_app(dependencies_from_env(ENV), probe))


def test_healthz_is_always_ok() -> None:
    every_dep_down = {"redis", "postgres"}
    assert _client(every_dep_down).get("/healthz").json() == {"status": "ok"}


def test_readyz_ok_when_everything_is_up() -> None:
    response = _client(set()).get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_dependencies_come_from_env() -> None:
    deps = dependencies_from_env(ENV)
    assert {d.name for d in deps} == {"postgres", "redis"}
    assert all(isinstance(d, Dependency) for d in deps)


def test_readyz_is_degraded_but_200_when_redis_is_down() -> None:
    response = _client({"redis"}).get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_readyz_is_503_when_postgres_is_down() -> None:
    response = _client({"postgres"}).get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
