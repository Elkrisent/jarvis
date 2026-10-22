from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from jarvis_api.db.migrate import to_async_url, upgrade
from jarvis_api.db.models import Base
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


def _diff(connection: Connection) -> list[object]:
    return compare_metadata(MigrationContext.configure(connection), Base.metadata)


async def test_models_describe_exactly_the_migrated_schema(database_url: str) -> None:
    await upgrade(database_url)
    engine = create_async_engine(to_async_url(database_url))
    try:
        async with engine.connect() as connection:
            diff = await connection.run_sync(_diff)
    finally:
        await engine.dispose()
    assert diff == []
