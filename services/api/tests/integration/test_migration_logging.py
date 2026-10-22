from __future__ import annotations

import logging

import pytest
from jarvis_api.db.migrate import run_migrations

pytestmark = [pytest.mark.integration, pytest.mark.req("REQ-12-2")]


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "jarvis_api.db.migrate"]


async def test_a_migrating_startup_logs_each_step(
    database_url, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="jarvis_api.db.migrate"):
        await run_migrations(database_url)

    messages = _messages(caplog)
    assert any("waiting for the migration lock" in m for m in messages)
    assert any("applying migrations" in m for m in messages)
    assert any("migrations complete" in m for m in messages)


async def test_an_up_to_date_startup_says_nothing_needed_applying(
    database_url, caplog: pytest.LogCaptureFixture
) -> None:
    await run_migrations(database_url)
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="jarvis_api.db.migrate"):
        await run_migrations(database_url)

    messages = _messages(caplog)
    assert any("already up to date" in m for m in messages)
    assert not any("applying migrations" in m for m in messages)


async def test_the_database_url_password_is_never_logged(
    database_url, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await run_migrations(database_url)

    password = database_url.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    assert password not in caplog.text
