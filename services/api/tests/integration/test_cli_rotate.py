"""`jarvis rotate-master-key` against a real database."""

from __future__ import annotations

import base64
import os
import subprocess
from collections.abc import AsyncIterator

import pytest
from jarvis_api.cli import main
from jarvis_api.db.migrate import to_async_url, upgrade
from jarvis_api.db.secrets import PostgresSecretStore
from jarvis_core.secretbox import AesGcmSecretBox
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = [pytest.mark.integration, pytest.mark.req("REQ-11.2-3")]

OLD_KEY = bytes(range(32))
NEW_KEY = bytes(range(100, 132))
OTHER_KEY = bytes(range(200, 232))
OLD_B64 = base64.b64encode(OLD_KEY).decode()
NEW_B64 = base64.b64encode(NEW_KEY).decode()


def _env(url: str) -> dict[str, str]:
    return {"JARVIS_MASTER_KEY": OLD_B64, "JARVIS_NEW_MASTER_KEY": NEW_B64, "DATABASE_URL": url}


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    await upgrade(database_url)
    engine = create_async_engine(to_async_url(database_url))
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed(engine: AsyncEngine, box: AesGcmSecretBox, count: int) -> list[str]:
    store = PostgresSecretStore(engine)
    return [await store.add(box, f"secret-{i}") for i in range(count)]


async def test_rotation_via_main_reencrypts_and_reports(
    engine: AsyncEngine, database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    old, new = AesGcmSecretBox(OLD_KEY), AesGcmSecretBox(NEW_KEY)
    ids = await _seed(engine, old, 3)

    code = await _run_main(_env(database_url))

    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == f"re-encrypted 3 secret(s) from {old.key_id} to {new.key_id}\n"
    assert captured.err == ""
    store = PostgresSecretStore(engine)
    for i, secret_id in enumerate(ids):
        assert await store.reveal(new, secret_id) == f"secret-{i}"
        with pytest.raises(KeyError):
            await store.reveal(old, secret_id)


async def test_rotation_aborts_with_exit_1_on_an_unknown_key_record(
    engine: AsyncEngine, database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    other = AesGcmSecretBox(OTHER_KEY)
    old = AesGcmSecretBox(OLD_KEY)
    [good] = await _seed(engine, old, 1)
    [stray] = await _seed(engine, other, 1)
    store = PostgresSecretStore(engine)
    before = (await store.get(good), await store.get(stray))

    code = await _run_main(_env(database_url))

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert other.key_id in captured.err
    assert OLD_B64 not in captured.err
    assert NEW_B64 not in captured.err
    assert (await store.get(good), await store.get(stray)) == before


def test_database_down_exits_1_without_leaking_keys(capsys: pytest.CaptureFixture[str]) -> None:
    url = "postgresql://jarvis_user:hunter2pw@127.0.0.1:1/nodb"

    code = main(["rotate-master-key"], env=_env(url))

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert "could not connect to the database" in captured.err
    combined = captured.out + captured.err
    for secret in (OLD_B64, NEW_B64, "hunter2pw", "jarvis_user"):
        assert secret not in combined


def test_console_script_runs(database_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    import asyncio

    asyncio.run(_prepare_with_one_secret(database_url))
    child_env = {**os.environ, **_env(database_url)}

    result = subprocess.run(
        ["uv", "run", "--locked", "jarvis", "rotate-master-key"],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    old, new = AesGcmSecretBox(OLD_KEY), AesGcmSecretBox(NEW_KEY)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"re-encrypted 1 secret(s) from {old.key_id} to {new.key_id}\n"


async def _prepare_with_one_secret(url: str) -> None:
    await upgrade(url)
    engine = create_async_engine(to_async_url(url))
    try:
        await _seed(engine, AesGcmSecretBox(OLD_KEY), 1)
    finally:
        await engine.dispose()


async def _run_main(env: dict[str, str]) -> int:
    # main() owns its own event loop (asyncio.run), so run it off this test's loop.
    import asyncio

    return await asyncio.to_thread(main, ["rotate-master-key"], env)
