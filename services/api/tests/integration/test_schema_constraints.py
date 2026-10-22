from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
from jarvis_api.db.migrate import upgrade

pytestmark = pytest.mark.integration


@pytest.fixture
async def conn(database_url: str, connect) -> AsyncIterator[asyncpg.Connection]:
    await upgrade(database_url)
    connection = await connect(database_url)
    try:
        await connection.execute(
            "INSERT INTO projects (id, name, slug) VALUES ('prj_1', 'One', 'one')"
        )
        yield connection
    finally:
        await connection.close()


async def _add_api(conn: asyncpg.Connection, api_id: str, project_id: str, slug: str) -> None:
    await conn.execute(
        "INSERT INTO api_dependencies (id, project_id, slug, name) VALUES ($1, $2, $3, 'n')",
        api_id,
        project_id,
        slug,
    )


async def test_project_slugs_are_unique(conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute("INSERT INTO projects (id, name, slug) VALUES ('prj_2', 'Dup', 'one')")


async def test_api_slugs_are_unique_within_a_project(conn: asyncpg.Connection) -> None:
    await _add_api(conn, "api_1", "prj_1", "weather")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _add_api(conn, "api_2", "prj_1", "weather")


async def test_the_same_api_slug_is_allowed_in_different_projects(
    conn: asyncpg.Connection,
) -> None:
    await conn.execute("INSERT INTO projects (id, name, slug) VALUES ('prj_2', 'Two', 'two')")
    await _add_api(conn, "api_1", "prj_1", "weather")
    await _add_api(conn, "api_2", "prj_2", "weather")


async def test_api_requires_an_existing_project(conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _add_api(conn, "api_1", "prj_missing", "weather")


async def test_key_requires_an_existing_project(conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute(
            "INSERT INTO jarvis_keys (id, project_id, name, prefix, hash)"
            " VALUES ('key_1', 'prj_missing', 'k', 'jv_live_ab', 'h1')"
        )


async def test_policy_versions_are_unique_per_api(conn: asyncpg.Connection) -> None:
    await _add_api(conn, "api_1", "prj_1", "weather")
    insert = (
        "INSERT INTO api_policy_versions (id, api_id, version, config_json)"
        " VALUES ($1, 'api_1', 1, '{}')"
    )
    await conn.execute(insert, "ver_1")
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(insert, "ver_2")


async def test_current_version_must_exist(conn: asyncpg.Connection) -> None:
    await _add_api(conn, "api_1", "prj_1", "weather")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute("UPDATE api_dependencies SET current_version_id = 'ver_missing'")


async def test_deleting_a_project_removes_its_apis_and_versions(conn: asyncpg.Connection) -> None:
    await _add_api(conn, "api_1", "prj_1", "weather")
    await conn.execute(
        "INSERT INTO api_policy_versions (id, api_id, version, config_json)"
        " VALUES ('ver_1', 'api_1', 1, '{}')"
    )
    await conn.execute("DELETE FROM projects WHERE id = 'prj_1'")
    assert await conn.fetchval("SELECT count(*) FROM api_dependencies") == 0
    assert await conn.fetchval("SELECT count(*) FROM api_policy_versions") == 0


async def test_user_role_is_restricted(conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, role)"
            " VALUES ('usr_1', 'a@b.c', 'x', 'superuser')"
        )
