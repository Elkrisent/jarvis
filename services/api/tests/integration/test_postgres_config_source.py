"""PostgresConfigSource against a real database, via the shared ConfigSource contract."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from jarvis_api.db.config_source import PostgresConfigSource
from jarvis_api.db.migrate import to_async_url, upgrade
from jarvis_core.config import ApiDependency
from jarvis_core.contracts import ConfigSourceContract
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine(database_url: str, connect) -> AsyncIterator[AsyncEngine]:
    await upgrade(database_url)
    conn = await connect(database_url)
    try:
        await conn.execute(
            "INSERT INTO projects (id, name, slug) VALUES"
            " ('prj_1', 'One', 'one'), ('prj_2', 'Two', 'two')"
        )
    finally:
        await conn.close()
    engine = create_async_engine(to_async_url(database_url))
    try:
        yield engine
    finally:
        await engine.dispose()


class TestPostgresConfigSourceContract(ConfigSourceContract):
    @pytest.fixture
    def source(self, engine: AsyncEngine) -> PostgresConfigSource:
        return PostgresConfigSource(engine)


def _api(name: str = "Weather") -> ApiDependency:
    return ApiDependency(slug="weather", name=name, base_url="https://api.weather.example")


async def test_every_put_is_kept_as_an_immutable_policy_version(
    engine: AsyncEngine, database_url: str, connect
) -> None:
    source = PostgresConfigSource(engine)
    await source.put("prj_1", _api("v1"))
    await source.put("prj_1", _api("v2"))

    conn = await connect(database_url)
    try:
        rows = await conn.fetch(
            "SELECT version, config_json->>'name' AS name FROM api_policy_versions ORDER BY version"
        )
        current = await conn.fetchval(
            "SELECT v.version FROM api_dependencies a"
            " JOIN api_policy_versions v ON v.id = a.current_version_id"
        )
    finally:
        await conn.close()
    assert [(r["version"], r["name"]) for r in rows] == [(1, "v1"), (2, "v2")]
    assert current == 2


async def test_config_survives_a_new_engine(engine: AsyncEngine, database_url: str) -> None:
    await PostgresConfigSource(engine).put("prj_1", _api())

    fresh = create_async_engine(to_async_url(database_url))
    try:
        assert await PostgresConfigSource(fresh).get("prj_1", "weather") == _api()
    finally:
        await fresh.dispose()


async def test_concurrent_puts_get_distinct_consecutive_versions(engine: AsyncEngine) -> None:
    source = PostgresConfigSource(engine)
    versions = await asyncio.gather(*(source.put("prj_1", _api(f"v{n}")) for n in range(8)))
    assert sorted(versions) == list(range(1, 9))


async def test_put_for_an_unknown_project_is_rejected(engine: AsyncEngine) -> None:
    with pytest.raises(LookupError, match="prj_missing"):
        await PostgresConfigSource(engine).put("prj_missing", _api())
