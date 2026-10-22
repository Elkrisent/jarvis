import base64
import dataclasses
import logging

import pytest
from jarvis_core.interfaces import SealedSecret
from jarvis_core.secretbox import AesGcmSecretBox, MasterKeyError, parse_master_key

PLAINTEXT = "PLAINTEXT-ZX81"
KEY = bytes(range(1, 33))
OTHER_KEY = bytes(range(101, 133))

pytestmark = pytest.mark.security


def _key_forms(key: bytes) -> list[str]:
    return [
        base64.b64encode(key).decode(),
        base64.urlsafe_b64encode(key).decode(),
        base64.b64encode(key).decode().rstrip("="),
        key.hex(),
        key.hex().upper(),
        repr(key),
        str(list(key)),
    ]


# Rejected inputs: a tool must never echo back a malformed key a user supplied.
BAD_SHORT = base64.b64encode(KEY[:16]).decode()
BAD_SUFFIXED = base64.b64encode(KEY).decode() + "!"


def _rejected_forms() -> list[str]:
    return [BAD_SHORT, BAD_SUFFIXED, *_key_forms(KEY[:16])]


def _assert_clean(text: str, key: bytes = KEY) -> None:
    assert PLAINTEXT not in text
    assert PLAINTEXT.encode().hex() not in text
    for form in [*_key_forms(key), *_rejected_forms()]:
        assert form not in text


@pytest.mark.req("REQ-11.2-4")
def test_leak_checker_detects_each_form() -> None:
    # Guards against vacuous assertions: the checker must fire on every form it claims to cover.
    for form in [PLAINTEXT, *_key_forms(KEY), *_rejected_forms()]:
        with pytest.raises(AssertionError):
            _assert_clean(f"prefix {form} suffix")


@pytest.mark.req("REQ-11.2-4")
def test_repr_and_str_of_box_and_sealed_secret_hide_plaintext_and_key() -> None:
    box = AesGcmSecretBox(KEY)
    sealed = box.seal(PLAINTEXT)
    for obj in (box, sealed):
        for text in (repr(obj), str(obj)):
            _assert_clean(text)
    assert repr(box) == f"AesGcmSecretBox(key_id={box.key_id!r})"
    assert str(box) == repr(box)
    assert dataclasses.is_dataclass(sealed)


@pytest.mark.req("REQ-11.2-4")
def test_logs_never_contain_plaintext_or_key(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    box = AesGcmSecretBox(KEY)
    other = AesGcmSecretBox(OTHER_KEY)

    sealed = box.seal(PLAINTEXT)
    assert box.open(sealed) == PLAINTEXT

    tampered = SealedSecret(
        sealed.key_id, sealed.nonce, bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]
    )
    with pytest.raises(ValueError, match="authentication"):
        box.open(tampered)
    with pytest.raises(KeyError):
        other.open(sealed)
    with pytest.raises(MasterKeyError):
        parse_master_key(BAD_SHORT)
    with pytest.raises(MasterKeyError):
        parse_master_key(BAD_SUFFIXED)
    with pytest.raises(MasterKeyError):
        AesGcmSecretBox(KEY[:16])

    _assert_clean(caplog.text)
    _assert_clean(caplog.text, OTHER_KEY)


@pytest.mark.req("REQ-11.2-4")
def test_exceptions_never_contain_plaintext_or_key() -> None:
    box = AesGcmSecretBox(KEY)
    other = AesGcmSecretBox(OTHER_KEY)
    sealed = box.seal(PLAINTEXT)
    tampered = SealedSecret(
        sealed.key_id, sealed.nonce, bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]
    )

    errors: list[Exception] = []
    for action in (
        lambda: box.open(tampered),
        lambda: other.open(sealed),
        lambda: parse_master_key(BAD_SHORT),
        lambda: parse_master_key(BAD_SUFFIXED),
        lambda: AesGcmSecretBox(KEY[:16]),
    ):
        try:
            action()
        except (ValueError, KeyError) as exc:  # MasterKeyError is a ValueError
            errors.append(exc)
        else:
            pytest.fail("expected the action to fail")

    assert {type(e) for e in errors} == {ValueError, KeyError, MasterKeyError}
    for exc in errors:
        _assert_clean(str(exc))
        _assert_clean(repr(exc))
        _assert_clean(str(exc), OTHER_KEY)
        _assert_clean(repr(exc), OTHER_KEY)
