"""Failure modes of ``run_migrations`` found in code review."""

from __future__ import annotations

import contextlib

import pytest
from jarvis_api.db import migrate
from jarvis_api.db.migrate import run_migrations
from sqlalchemy import text
from sqlalchemy.engine import Connection

pytestmark = [pytest.mark.integration, pytest.mark.req("REQ-12-2")]


async def test_database_url_with_sslmode_is_accepted(
    database_url, list_tables, m1_1_tables
) -> None:
    # libpq-style options are common in managed-Postgres URLs; asyncpg spells it ``ssl``.
    await run_migrations(f"{database_url}?sslmode=prefer")
    assert await list_tables(database_url) == m1_1_tables


async def test_a_database_ahead_of_this_code_is_left_alone(database_url, connect) -> None:
    # An older instance may start after a newer one has already migrated further.
    await run_migrations(database_url)
    conn = await connect(database_url)
    try:
        await conn.execute("UPDATE alembic_version SET version_num = '9999'")
    finally:
        await conn.close()

    calls: list[str] = []

    async def on_upgrade() -> None:
        calls.append("upgrade")

    await run_migrations(database_url, on_upgrade=on_upgrade)

    assert calls == []


async def test_a_dropped_connection_does_not_mask_the_migration_error(
    database_url, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drop_connection_then_fail(connection: Connection, revision: str) -> None:
        with contextlib.suppress(Exception):
            connection.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))
        raise RuntimeError("migration blew up")

    monkeypatch.setattr(migrate, "_upgrade", drop_connection_then_fail)

    with pytest.raises(RuntimeError, match="migration blew up"):
        await run_migrations(database_url)


async def test_a_dead_connection_during_cleanup_does_not_mask_the_migration_error(
    database_url, monkeypatch
) -> None:
    class Boom(Exception):
        pass

    def fail(connection: Connection, revision: str) -> None:
        raise Boom

    async def dead(self: object) -> None:
        raise ConnectionError("connection lost")

    monkeypatch.setattr(migrate, "_upgrade", fail)
    # The cleanup in ``finally`` then fails too, as it would on a dropped connection.
    monkeypatch.setattr(migrate.AsyncConnection, "rollback", dead)

    with pytest.raises(Boom):
        await run_migrations(database_url)
