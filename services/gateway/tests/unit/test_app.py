from typing import Literal, cast

import pytest
from fastapi.testclient import TestClient
from jarvis_core.clock import FakeClock
from jarvis_core.health import Dependency
from jarvis_gateway.app import create_app, create_app_from_env, dependencies_from_env
from jarvis_gateway.config.manager import ConfigManager, ConfigStatus

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
    assert {d.name for d in deps} == {"redis"}
    assert all(isinstance(d, Dependency) for d in deps)


def test_readyz_is_degraded_but_200_when_redis_is_down() -> None:
    response = _client({"redis"}).get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "checks": {"redis": False}}


# --- config readiness (M1.4) -------------------------------------------------------------------

INTERVAL = 10.0


class FakeManager:
    def __init__(self, status: ConfigStatus) -> None:
        self._status = status
        self.events: list[str] = []

    def status(self) -> ConfigStatus:
        return self._status

    async def start(self) -> None:
        self.events.append("start")

    async def stop(self) -> None:
        self.events.append("stop")


def _managed(
    status: ConfigStatus, clock: FakeClock | None = None, down: set[str] | None = None
) -> tuple[TestClient, FakeManager]:
    async def probe(host: str, port: int) -> bool:
        return host not in (down or set())

    manager = FakeManager(status)
    app = create_app(
        dependencies_from_env(ENV),
        probe,
        config=cast(ConfigManager, manager),
        resync_interval_s=INTERVAL,
        clock=clock or FakeClock(1000.0),
    )
    return TestClient(app), manager


def _status(source: Literal["postgres", "file"], synced: float, ok: float | None) -> ConfigStatus:
    return ConfigStatus(source, 3, 2, synced, ok)


@pytest.mark.req("REQ-8-8")
def test_readyz_is_unavailable_when_nothing_is_loaded() -> None:
    client, _ = _managed(ConfigStatus("none", 0, 0, None, None))
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["config"] == {"source": "none", "apis": 0, "keys": 0, "synced_at": None}


@pytest.mark.req("REQ-8-9")
def test_readyz_is_degraded_but_200_when_serving_the_snapshot_file() -> None:
    client, _ = _managed(_status("file", 900.0, None))
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["config"]["source"] == "file"


@pytest.mark.req("REQ-8-2")
def test_readyz_is_degraded_when_the_last_full_sync_is_older_than_three_intervals() -> None:
    clock = FakeClock(1000.0)
    client, _ = _managed(_status("postgres", 990.0, 990.0), clock)
    assert client.get("/readyz").json()["status"] == "ok"
    clock.advance(3 * INTERVAL - 10)  # exactly 3 intervals old: still fresh
    assert client.get("/readyz").json()["status"] == "ok"
    clock.advance(0.5)
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["config"]["source"] == "postgres"  # Postgres loss keeps serving


def test_readyz_is_ok_with_a_fresh_postgres_sync_and_reports_counts_only() -> None:
    client, _ = _managed(_status("postgres", 995.0, 995.0))
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"redis": True},
        "config": {"source": "postgres", "apis": 3, "keys": 2, "synced_at": 995.0},
    }


def test_redis_down_still_degrades_a_fresh_gateway() -> None:
    client, _ = _managed(_status("postgres", 995.0, 995.0), down={"redis"})
    assert client.get("/readyz").json()["status"] == "degraded"


def test_the_manager_runs_with_the_app_lifespan_and_healthz_is_always_200() -> None:
    client, manager = _managed(ConfigStatus("none", 0, 0, None, None))
    with client:
        assert manager.events == ["start"]
        assert client.get("/healthz").status_code == 200
    assert manager.events == ["start", "stop"]


def test_stop_still_runs_when_start_fails() -> None:
    client, manager = _managed(ConfigStatus("none", 0, 0, None, None))

    async def boom() -> None:
        manager.events.append("start")
        raise RuntimeError("start failed")

    manager.start = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="start failed"), client:
        pass
    assert manager.events == ["start", "stop"]


def test_postgres_source_without_any_recorded_sync_is_degraded() -> None:
    client, _ = _managed(_status("postgres", 995.0, None))
    assert client.get("/readyz").json()["status"] == "degraded"


@pytest.mark.req("REQ-8-2")
def test_create_app_from_env_wires_a_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", ENV["DATABASE_URL"])
    monkeypatch.setenv("REDIS_URL", ENV["REDIS_URL"])
    monkeypatch.setenv("JARVIS_SNAPSHOT_PATH", "/nonexistent/snapshot.json")
    app = create_app_from_env()
    assert app.title == "jarvis-gateway"


def test_the_lifespan_without_a_config_manager_just_serves() -> None:
    async def probe(host: str, port: int) -> bool:
        return True

    with TestClient(create_app(dependencies_from_env(ENV), probe)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
