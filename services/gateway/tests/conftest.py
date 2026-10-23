"""Fixtures for gateway tests (integration and bench) that need a real Postgres.

``DATABASE_URL`` must point at a server whose role may create databases and roles. Each test gets
its own throwaway, fully migrated database (the schema comes from ``jarvis_api.db.migrate``, which
is test-only here: the gateway never imports ``jarvis_api`` in ``src``).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
from jarvis_api.db.migrate import upgrade
from sqlalchemy.engine import make_url

ADMIN_URL_ENV = "DATABASE_URL"


def asyncpg_dsn(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
async def database_url() -> AsyncIterator[str]:
    """URL of a freshly created, migrated database that is dropped after the test."""
    admin_url = os.environ.get(ADMIN_URL_ENV)
    if not admin_url:
        pytest.fail(f"{ADMIN_URL_ENV} must point at a Postgres server to run integration tests")
    name = f"jarvis_gw_it_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(asyncpg_dsn(admin_url))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
        await upgrade(url)
        yield url
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
