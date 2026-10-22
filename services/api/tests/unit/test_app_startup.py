from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from jarvis_api import app as app_module
from jarvis_api.app import create_app, create_app_from_env

pytestmark = [pytest.mark.unit, pytest.mark.req("REQ-12-2")]

DATABASE_URL = "postgresql://u:p@postgres:5432/jarvis"


def test_migrations_run_once_before_the_app_serves() -> None:
    calls: list[str] = []

    async def migrate() -> None:
        calls.append("migrate")

    with TestClient(create_app(migrate=migrate)) as client:
        assert calls == ["migrate"]
        assert client.get("/healthz").status_code == 200
    assert calls == ["migrate"]


def test_a_failed_migration_stops_startup() -> None:
    async def migrate() -> None:
        raise RuntimeError("migration failed")

    with (
        pytest.raises(RuntimeError, match="migration failed"),
        TestClient(create_app(migrate=migrate)),
    ):
        pytest.fail("the app must not start serving after a failed migration")


def test_app_without_a_migrate_hook_still_starts() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200


def test_env_app_makes_its_info_logs_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    # uvicorn configures only its own loggers, so without this the migration log lines never show.
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    logger = logging.getLogger("jarvis_api")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logging.NOTSET)

    create_app_from_env()
    create_app_from_env()

    assert logger.level == logging.INFO
    assert len(logger.handlers) == 1


def test_env_app_migrates_the_configured_database(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake_run_migrations(url: str) -> None:
        seen.append(url)

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setattr(app_module, "run_migrations", fake_run_migrations)

    with TestClient(create_app_from_env()):
        pass

    assert seen == [DATABASE_URL]
