"""Fixtures for api integration tests that need a real Postgres.

``DATABASE_URL`` must point at a server whose role may create databases. Each test gets its own
throwaway database, so tests never see each other's data and nothing existing is touched.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import asyncpg
import pytest
from sqlalchemy.engine import make_url

ADMIN_URL_ENV = "DATABASE_URL"

ListTables = Callable[[str], Awaitable[set[str]]]
Connect = Callable[[str], Awaitable[asyncpg.Connection]]


def _admin_url() -> str:
    url = os.environ.get(ADMIN_URL_ENV)
    if not url:
        pytest.fail(f"{ADMIN_URL_ENV} must point at a Postgres server to run integration tests")
    return url


def _asyncpg_dsn(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
async def database_url() -> AsyncIterator[str]:
    """A URL for a freshly created, empty database that is dropped after the test."""
    admin_url = _admin_url()
    name = f"jarvis_it_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_asyncpg_dsn(admin_url))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        yield make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.fixture
def connect() -> Connect:
    """Open a raw asyncpg connection to a database URL (the caller closes it)."""

    async def _connect(url: str) -> asyncpg.Connection:
        return await asyncpg.connect(_asyncpg_dsn(url))

    return _connect


@pytest.fixture
def list_tables(connect: Connect) -> ListTables:
    """Public tables in a database, excluding Alembic's own bookkeeping table."""

    async def _list(url: str) -> set[str]:
        conn = await connect(url)
        try:
            rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        finally:
            await conn.close()
        return {r["tablename"] for r in rows} - {"alembic_version"}

    return _list


@pytest.fixture
def m1_1_tables() -> set[str]:
    return {
        "users",
        "projects",
        "jarvis_keys",
        "api_dependencies",
        "api_policy_versions",
        "secrets",
        "settings",
        "audit_log",
    }
