"""PostgresSecretStore against a real database: round trip and atomic master-key rotation."""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator

import pytest
from jarvis_api.db.migrate import to_async_url, upgrade
from jarvis_api.db.secrets import PostgresSecretStore, RotationError
from jarvis_core.secretbox import AesGcmSecretBox
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.integration

OLD_KEY = bytes(range(32))
NEW_KEY = bytes(range(100, 132))
OTHER_KEY = bytes(range(200, 232))


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    await upgrade(database_url)
    engine = create_async_engine(to_async_url(database_url))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def store(engine: AsyncEngine) -> PostgresSecretStore:
    return PostgresSecretStore(engine)


@pytest.fixture
def old() -> AesGcmSecretBox:
    return AesGcmSecretBox(OLD_KEY)


@pytest.fixture
def new() -> AesGcmSecretBox:
    return AesGcmSecretBox(NEW_KEY)


async def _rows(database_url: str, connect) -> list[tuple[str, str, bytes, bytes]]:
    conn = await connect(database_url)
    try:
        records = await conn.fetch("SELECT id, key_id, nonce, ciphertext FROM secrets ORDER BY id")
    finally:
        await conn.close()
    return [(r["id"], r["key_id"], bytes(r["nonce"]), bytes(r["ciphertext"])) for r in records]


@pytest.mark.req("REQ-11.2-2")
async def test_add_then_get_round_trips_a_sealed_secret(
    store: PostgresSecretStore, old: AesGcmSecretBox
) -> None:
    secret_id = await store.add(old, "sk-live-abc")

    assert secret_id.startswith("sec_")
    sealed = await store.get(secret_id)
    assert sealed is not None
    assert sealed.key_id == old.key_id
    assert await store.reveal(old, secret_id) == "sk-live-abc"
    assert await store.get("sec_missing") is None
    assert await store.reveal(old, "sec_missing") is None


@pytest.mark.req("REQ-11.2-3")
async def test_rotate_reencrypts_every_record(
    store: PostgresSecretStore,
    old: AesGcmSecretBox,
    new: AesGcmSecretBox,
    database_url: str,
    connect,
) -> None:
    plaintexts = {}
    for n in range(5):
        plaintexts[await store.add(old, f"secret-{n}")] = f"secret-{n}"
    before = {row[0]: row for row in await _rows(database_url, connect)}

    assert await store.rotate(old, new) == 5

    after = await _rows(database_url, connect)
    assert len(after) == 5
    for secret_id, key_id, nonce, _ in after:
        assert key_id == new.key_id
        assert nonce != before[secret_id][2]
        assert await store.reveal(new, secret_id) == plaintexts[secret_id]


@pytest.mark.req("REQ-11.2-3")
async def test_old_key_can_no_longer_decrypt_after_rotation(
    store: PostgresSecretStore, old: AesGcmSecretBox, new: AesGcmSecretBox
) -> None:
    secret_id = await store.add(old, "sk-live-abc")
    await store.rotate(old, new)

    rotated = await store.get(secret_id)
    assert rotated is not None
    with pytest.raises(KeyError):
        old.open(rotated)


@pytest.mark.req("REQ-11.2-3")
async def test_rotation_with_no_secrets_rewrites_nothing(
    store: PostgresSecretStore, old: AesGcmSecretBox, new: AesGcmSecretBox
) -> None:
    assert await store.rotate(old, new) == 0


@pytest.mark.req("REQ-11.2-3")
async def test_rotating_twice_rewrites_nothing_the_second_time(
    store: PostgresSecretStore,
    old: AesGcmSecretBox,
    new: AesGcmSecretBox,
    database_url: str,
    connect,
) -> None:
    await store.add(old, "a")
    await store.add(old, "b")
    assert await store.rotate(old, new) == 2
    after_first = await _rows(database_url, connect)

    assert await store.rotate(old, new) == 0
    assert await _rows(database_url, connect) == after_first


@pytest.mark.req("REQ-11.2-3")
async def test_a_record_sealed_with_an_unknown_key_aborts_the_rotation_and_changes_nothing(
    store: PostgresSecretStore,
    old: AesGcmSecretBox,
    new: AesGcmSecretBox,
    database_url: str,
    connect,
) -> None:
    other = AesGcmSecretBox(OTHER_KEY)
    for n in range(3):
        await store.add(old, f"secret-{n}")
    await store.add(other, "foreign")
    before = await _rows(database_url, connect)

    with pytest.raises(RotationError) as excinfo:
        await store.rotate(old, new)

    assert other.key_id in str(excinfo.value)
    assert "1" in str(excinfo.value)
    assert await _rows(database_url, connect) == before


@pytest.mark.req("REQ-11.2-4")
async def test_rotation_error_and_logs_contain_no_key_material_or_plaintext(
    store: PostgresSecretStore,
    engine: AsyncEngine,
    old: AesGcmSecretBox,
    new: AesGcmSecretBox,
    caplog: pytest.LogCaptureFixture,
) -> None:
    other = AesGcmSecretBox(OTHER_KEY)
    plaintexts = ["plain-alpha-1", "plain-beta-2"]
    for plaintext in plaintexts:
        await store.add(old, plaintext)
    await store.add(other, "plain-foreign-3")

    caplog.set_level(logging.INFO)
    with pytest.raises(RotationError) as failed:
        await store.rotate(old, new)
    # Remove the foreign record so a rotation can succeed and log too.
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM secrets WHERE key_id = :k"), {"k": other.key_id})
    assert await store.rotate(old, new) == 2

    output = str(failed.value) + caplog.text
    assert new.key_id in caplog.text
    assert old.key_id in output
    assert other.key_id in output
    for plaintext in [*plaintexts, "plain-foreign-3"]:
        assert plaintext not in output
    for key in (OLD_KEY, NEW_KEY, OTHER_KEY):
        assert key.hex() not in output
        assert base64.b64encode(key).decode() not in output
        assert base64.urlsafe_b64encode(key).decode() not in output
        assert key.decode("latin-1") not in output


@pytest.mark.req("REQ-11.2-3")
async def test_a_tampered_record_aborts_the_rotation_and_rolls_back_earlier_updates(
    store: PostgresSecretStore,
    old: AesGcmSecretBox,
    new: AesGcmSecretBox,
    database_url: str,
    connect,
) -> None:
    for n in range(4):
        await store.add(old, f"plain-tamper-{n}")
    before = await _rows(database_url, connect)
    # Rows are rewritten in id order, so the first two are updated before the third fails.
    victim_id, _, _, victim_ciphertext = before[2]
    flipped = bytes([victim_ciphertext[0] ^ 0xFF]) + victim_ciphertext[1:]
    conn = await connect(database_url)
    try:
        await conn.execute("UPDATE secrets SET ciphertext = $1 WHERE id = $2", flipped, victim_id)
    finally:
        await conn.close()
    tampered = await _rows(database_url, connect)

    with pytest.raises(RotationError) as excinfo:
        await store.rotate(old, new)

    message = str(excinfo.value)
    assert victim_id in message
    assert old.key_id in message
    assert "nothing was changed" in message
    for secret in (*(f"plain-tamper-{n}" for n in range(4)), *(OLD_KEY, NEW_KEY)):
        if isinstance(secret, bytes):
            assert secret.hex() not in message
            assert base64.b64encode(secret).decode() not in message
        else:
            assert secret not in message
    assert await _rows(database_url, connect) == tampered


@pytest.mark.req("REQ-11.2-2")
async def test_a_record_copied_to_another_row_fails_authentication(
    store: PostgresSecretStore, old: AesGcmSecretBox, database_url: str, connect
) -> None:
    weather = await store.add(old, "weather-credential")
    payments = await store.add(old, "payments-credential")
    conn = await connect(database_url)
    try:
        await conn.execute(
            "UPDATE secrets SET nonce = s.nonce, ciphertext = s.ciphertext "
            "FROM secrets s WHERE s.id = $1 AND secrets.id = $2",
            weather,
            payments,
        )
    finally:
        await conn.close()

    assert await store.reveal(old, weather) == "weather-credential"
    with pytest.raises(ValueError, match="authentication"):
        await store.reveal(old, payments)


@pytest.mark.req("REQ-11.2-3")
async def test_rotation_keeps_each_record_bound_to_its_row(
    store: PostgresSecretStore, old: AesGcmSecretBox, new: AesGcmSecretBox
) -> None:
    first = await store.add(old, "one")
    second = await store.add(old, "two")
    await store.rotate(old, new)

    assert await store.reveal(new, first) == "one"
    assert await store.reveal(new, second) == "two"
