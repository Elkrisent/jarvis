from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from jarvis_api.app import create_app_from_env

pytestmark = pytest.mark.integration


async def test_api_startup_migrates_an_empty_database(
    database_url, list_tables, m1_1_tables, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    assert await list_tables(database_url) == set()

    with TestClient(create_app_from_env()) as client:
        assert client.get("/healthz").status_code == 200

    assert await list_tables(database_url) == m1_1_tables
