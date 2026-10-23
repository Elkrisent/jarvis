"""PostgresLoader paths that need no database: faked pools and malformed DSNs."""

from __future__ import annotations

import asyncpg
import pytest
from jarvis_gateway.config import postgres
from jarvis_gateway.config.postgres import ConfigLoadError, PostgresLoader

pytestmark = pytest.mark.unit

DSN = "postgresql://user:SECRET-MARKER@127.0.0.1:5432/db"


def test_repr_never_includes_the_dsn() -> None:
    assert repr(PostgresLoader(DSN)) == "PostgresLoader()"


def test_a_dsn_urlsplit_cannot_parse_is_left_for_asyncpg_to_reject() -> None:
    assert postgres._asyncpg_dsn("postgresql://[::1/db") == "postgresql://[::1/db"


async def test_a_value_error_from_create_pool_is_an_invalid_dsn_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def create_pool(*args: object, **kwargs: object) -> asyncpg.Pool:
        raise ValueError(DSN)

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    with pytest.raises(ConfigLoadError, match="invalid database DSN") as info:
        await PostgresLoader(DSN).load_api("a1")
    assert "SECRET-MARKER" not in str(info.value)


async def test_reset_without_a_pool_is_a_no_op() -> None:
    loader = PostgresLoader(DSN)
    await loader._reset()  # nothing was ever opened
    await loader.close()


async def test_a_pool_that_fails_to_close_is_terminated(monkeypatch: pytest.MonkeyPatch) -> None:
    terminated: list[bool] = []

    class StuckPool:
        async def close(self) -> None:
            raise OSError("connection reset")

        def terminate(self) -> None:
            terminated.append(True)

    loader = PostgresLoader(DSN)
    loader._pool = StuckPool()  # type: ignore[assignment]
    await loader.close()
    assert terminated == [True]
    assert loader._pool is None
