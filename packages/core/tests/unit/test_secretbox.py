"""AES-256-GCM SecretBox and master-key parsing."""

import base64
import re
from dataclasses import replace

import pytest
from jarvis_core.contracts import SecretBoxContract
from jarvis_core.interfaces import SecretBox
from jarvis_core.secretbox import (
    MASTER_KEY_ENV,
    AesGcmSecretBox,
    MasterKeyError,
    key_id_for,
    parse_master_key,
    secret_box_from_env,
)

KEY = bytes(range(32))
OTHER_KEY = bytes(reversed(range(32)))
# Standard base64 of this key contains '+' and '/', so the URL-safe spelling differs.
SLASHY_KEY = bytes([0xFB, 0xFF]) * 16


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-2")
class TestAesGcmSecretBox(SecretBoxContract):
    @pytest.fixture
    def box(self) -> SecretBox:
        return AesGcmSecretBox(KEY)


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-2")
def test_nonce_is_12_bytes_and_ciphertext_is_plaintext_plus_16_byte_tag() -> None:
    sealed = AesGcmSecretBox(KEY).seal("abc")
    assert len(sealed.nonce) == 12
    assert len(sealed.ciphertext) == 3 + 16


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-2")
def test_tampered_nonce_fails_authentication() -> None:
    box = AesGcmSecretBox(KEY)
    sealed = box.seal("s3cret")
    nonce = bytes([sealed.nonce[0] ^ 1]) + sealed.nonce[1:]
    with pytest.raises(ValueError, match="authentication"):
        box.open(replace(sealed, nonce=nonce))


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-2")
def test_relabelling_the_key_id_fails_authentication() -> None:
    box = AesGcmSecretBox(KEY)
    foreign = AesGcmSecretBox(OTHER_KEY).seal("s3cret")
    with pytest.raises(ValueError, match="authentication"):
        box.open(replace(foreign, key_id=box.key_id))


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-2")
def test_a_box_cannot_open_another_keys_records() -> None:
    sealed = AesGcmSecretBox(KEY).seal("s3cret")
    with pytest.raises(KeyError):
        AesGcmSecretBox(OTHER_KEY).open(sealed)


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-2")
def test_key_id_is_derived_from_the_key() -> None:
    box = AesGcmSecretBox(KEY)
    assert box.key_id == key_id_for(KEY)
    assert re.fullmatch(r"k_[0-9a-f]{8}", box.key_id)
    assert AesGcmSecretBox(OTHER_KEY).key_id != box.key_id
    assert AesGcmSecretBox(KEY).key_id == box.key_id


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-2")
@pytest.mark.parametrize("plaintext", ["", "🔑𝔘ñ", "x" * 1_000_000])
def test_round_trips_empty_non_bmp_and_one_megabyte_plaintexts(plaintext: str) -> None:
    box = AesGcmSecretBox(KEY)
    assert box.open(box.seal(plaintext)) == plaintext


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-2")
def test_repr_and_str_never_reveal_the_key() -> None:
    box = AesGcmSecretBox(KEY)
    assert repr(box) == f"AesGcmSecretBox(key_id={box.key_id!r})"
    assert str(box) == repr(box)
    assert _b64(KEY) not in repr(box)
    assert KEY.hex() not in repr(box)


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-1")
@pytest.mark.parametrize("size", [16, 33])
def test_constructor_rejects_keys_that_are_not_32_bytes(size: int) -> None:
    with pytest.raises(MasterKeyError):
        AesGcmSecretBox(bytes(size))


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-1")
def test_parse_master_key_accepts_32_byte_base64_with_surrounding_whitespace() -> None:
    encoded = _b64(KEY)
    assert parse_master_key(encoded + "\n") == KEY
    assert parse_master_key("  " + encoded + "  ") == KEY


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-1")
@pytest.mark.parametrize(
    "value",
    [
        "",
        "not base64!!",
        _b64(bytes(31)),
        _b64(bytes(33)),
        base64.urlsafe_b64encode(SLASHY_KEY).decode(),
    ],
    ids=["empty", "not-base64", "31-bytes", "33-bytes", "urlsafe"],
)
def test_parse_master_key_rejects_bad_values_without_echoing_them(value: str) -> None:
    with pytest.raises(MasterKeyError) as excinfo:
        parse_master_key(value)
    if value:
        assert value not in str(excinfo.value)
    assert "32 bytes" in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-1")
def test_urlsafe_fixture_really_differs_from_the_standard_spelling() -> None:
    standard = _b64(SLASHY_KEY)
    assert "+" in standard or "/" in standard
    assert base64.urlsafe_b64encode(SLASHY_KEY).decode() != standard
    assert parse_master_key(standard) == SLASHY_KEY


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-1")
def test_secret_box_from_env_reads_the_master_key() -> None:
    box = secret_box_from_env({MASTER_KEY_ENV: _b64(KEY)})
    assert box.key_id == key_id_for(KEY)
    assert box.open(AesGcmSecretBox(KEY).seal("hello")) == "hello"


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-1")
def test_secret_box_from_env_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MASTER_KEY_ENV, _b64(KEY))
    assert secret_box_from_env().key_id == key_id_for(KEY)


@pytest.mark.unit
@pytest.mark.req("REQ-11.2-1")
def test_secret_box_from_env_names_the_variable_when_missing() -> None:
    with pytest.raises(MasterKeyError, match="JARVIS_MASTER_KEY"):
        secret_box_from_env({})
