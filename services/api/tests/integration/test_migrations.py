from __future__ import annotations

import pytest
from jarvis_api.db.migrate import downgrade, upgrade

pytestmark = pytest.mark.integration


async def test_upgrade_creates_the_m1_1_tables(database_url, list_tables, m1_1_tables) -> None:
    await upgrade(database_url)
    assert await list_tables(database_url) == m1_1_tables


async def test_downgrade_to_base_removes_every_table(database_url, list_tables) -> None:
    await upgrade(database_url)
    await downgrade(database_url)
    assert await list_tables(database_url) == set()


async def test_upgrade_again_after_downgrade_restores_the_schema(
    database_url, list_tables, m1_1_tables
) -> None:
    await upgrade(database_url)
    await downgrade(database_url)
    await upgrade(database_url)
    assert await list_tables(database_url) == m1_1_tables
