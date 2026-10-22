from __future__ import annotations

import asyncio

import pytest
from jarvis_api.db.migrate import run_migrations

pytestmark = [pytest.mark.integration, pytest.mark.req("REQ-12-2")]


class UpgradeCounter:
    """Records how often a runner decided it had to apply migrations."""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls = 0
        self._delay = delay

    async def __call__(self) -> None:
        self.calls += 1
        # Hold the critical section open long enough for a second runner to arrive.
        await asyncio.sleep(self._delay)


async def test_run_migrations_brings_an_empty_database_to_head(
    database_url, list_tables, m1_1_tables
) -> None:
    await run_migrations(database_url)
    assert await list_tables(database_url) == m1_1_tables


async def test_run_migrations_is_a_no_op_when_already_at_head(database_url) -> None:
    await run_migrations(database_url)
    counter = UpgradeCounter()
    await run_migrations(database_url, on_upgrade=counter)
    assert counter.calls == 0


async def test_concurrent_startups_apply_migrations_exactly_once(
    database_url, list_tables, m1_1_tables
) -> None:
    counter = UpgradeCounter(delay=0.5)
    await asyncio.gather(
        run_migrations(database_url, on_upgrade=counter),
        run_migrations(database_url, on_upgrade=counter),
    )
    assert counter.calls == 1
    assert await list_tables(database_url) == m1_1_tables


async def test_many_concurrent_startups_still_apply_migrations_once(database_url) -> None:
    counter = UpgradeCounter(delay=0.3)
    await asyncio.gather(*(run_migrations(database_url, on_upgrade=counter) for _ in range(5)))
    assert counter.calls == 1
