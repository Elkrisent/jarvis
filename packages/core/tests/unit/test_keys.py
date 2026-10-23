"""JARVIS API key generation and hashing (REQ-11.1-6)."""

import hashlib
import re
import string

import pytest
from jarvis_core.keys import (
    DISPLAY_PREFIX_LEN,
    KEY_PREFIX,
    generate_jarvis_key,
    hash_jarvis_key,
    looks_like_jarvis_key,
)


@pytest.mark.unit
@pytest.mark.req("REQ-11.1-6")
def test_key_format_and_prefix() -> None:
    generated = generate_jarvis_key()
    assert re.fullmatch(r"jv_live_[0-9A-Za-z]{40,44}", generated.full)
    assert generated.full.startswith(KEY_PREFIX)
    assert generated.prefix == generated.full[:DISPLAY_PREFIX_LEN]
    assert DISPLAY_PREFIX_LEN == 12


@pytest.mark.unit
@pytest.mark.req("REQ-11.1-6")
def test_hash_is_sha256_hex_of_full_key() -> None:
    generated = generate_jarvis_key()
    assert generated.hash == hashlib.sha256(generated.full.encode()).hexdigest()
    assert hash_jarvis_key(generated.full) == generated.hash
    assert hash_jarvis_key("abc") == hash_jarvis_key("abc")


@pytest.mark.unit
@pytest.mark.req("REQ-11.1-6")
def test_generated_keys_are_distinct() -> None:
    assert len({generate_jarvis_key().full for _ in range(1000)}) == 1000


@pytest.mark.unit
@pytest.mark.req("REQ-11.1-6")
def test_repr_shows_only_prefix() -> None:
    generated = generate_jarvis_key()
    text = repr(generated)
    assert generated.prefix in text
    assert generated.full[DISPLAY_PREFIX_LEN:] not in text
    assert generated.hash not in text


@pytest.mark.unit
@pytest.mark.req("REQ-11.1-6")
def test_all_base62_character_classes_appear() -> None:
    body = "".join(generate_jarvis_key().full[len(KEY_PREFIX) :] for _ in range(200))
    assert set(body) & set(string.digits)
    assert set(body) & set(string.ascii_uppercase)
    assert set(body) & set(string.ascii_lowercase)


@pytest.mark.unit
@pytest.mark.req("REQ-11.1-6")
def test_looks_like_jarvis_key() -> None:
    assert looks_like_jarvis_key("jv_live_x")
    assert not looks_like_jarvis_key("sk-abc")
    assert not looks_like_jarvis_key("")
