"""Programmatic Alembic runner."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Arbitrary constant shared by every api instance, so they serialise on one Postgres lock.
MIGRATION_LOCK_KEY = 0x4A56_0001

OnUpgrade = Callable[[], Awaitable[None]]


def to_async_url(url: str) -> str:
    """Rewrite a plain ``postgresql://`` URL to use the asyncpg driver.

    asyncpg spells libpq's ``sslmode`` option ``ssl`` and rejects the former, so it is renamed.
    """
    parsed = make_url(url).set(drivername="postgresql+asyncpg")
    query = dict(parsed.query)
    if "sslmode" in query:
        sslmode = query.pop("sslmode")
        query.setdefault("ssl", sslmode)
    return parsed.set(query=query).render_as_string(hide_password=False)


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    return config


def _upgrade(connection: Connection, revision: str) -> None:
    command.upgrade(_config(connection), revision)


def _downgrade(connection: Connection, revision: str) -> None:
    command.downgrade(_config(connection), revision)


def _needs_upgrade(connection: Connection) -> bool:
    """False when the database is at head, or ahead of this code (a newer instance migrated it)."""
    script = ScriptDirectory.from_config(_config(connection))
    current = MigrationContext.configure(connection).get_current_revision()
    if current is None:
        return True
    if current == script.get_current_head():
        return False
    try:
        script.get_revision(current)
    except CommandError:
        return False
    return True


@asynccontextmanager
async def _connection(url: str) -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(to_async_url(url))
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


async def upgrade(url: str, revision: str = "head") -> None:
    async with _connection(url) as connection:
        await connection.run_sync(_upgrade, revision)
        await connection.commit()


async def downgrade(url: str, revision: str = "base") -> None:
    async with _connection(url) as connection:
        await connection.run_sync(_downgrade, revision)
        await connection.commit()


async def run_migrations(url: str, on_upgrade: OnUpgrade | None = None) -> None:
    """Bring the database to head, exactly once across concurrently starting instances.

    Instances queue on a Postgres advisory lock; the first applies the migrations and the rest
    find the database already at head. ``on_upgrade`` is awaited only by a runner that is about
    to apply migrations.
    """
    async with _connection(url) as connection:
        await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
        try:
            if await connection.run_sync(_needs_upgrade):
                if on_upgrade is not None:
                    await on_upgrade()
                await connection.run_sync(_upgrade, "head")
            await connection.commit()
        finally:
            await connection.rollback()
            await connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY}
            )
            await connection.commit()
