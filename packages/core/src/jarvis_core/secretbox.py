"""AES-256-GCM implementation of the ``SecretBox`` protocol, and master-key parsing."""

import base64
import binascii
import hashlib
import os
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jarvis_core.interfaces import SealedSecret

MASTER_KEY_ENV = "JARVIS_MASTER_KEY"

_KEY_BYTES = 32
_NONCE_BYTES = 12
_EXPECTED = "a master key must be 32 bytes, base64-encoded (standard alphabet)"


class MasterKeyError(ValueError):
    """The master key is missing or malformed. Never contains the key material."""


def key_id_for(key: bytes) -> str:
    return "k_" + hashlib.sha256(key).hexdigest()[:8]


def parse_master_key(value: str) -> bytes:
    """Decode a base64 master key into exactly 32 raw bytes."""
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise MasterKeyError(f"invalid master key: {_EXPECTED}") from None
    if len(raw) != _KEY_BYTES:
        raise MasterKeyError(f"invalid master key: {_EXPECTED}")
    return raw


class AesGcmSecretBox:
    """Seals secrets with AES-256-GCM; the key id is bound in as associated data."""

    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_BYTES:
            raise MasterKeyError(f"invalid master key: {_EXPECTED}")
        self._aead = AESGCM(key)
        self._key_id = key_id_for(key)

    @property
    def key_id(self) -> str:
        return self._key_id

    def __repr__(self) -> str:
        return f"AesGcmSecretBox(key_id={self._key_id!r})"

    def seal(self, plaintext: str) -> SealedSecret:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, plaintext.encode(), self._key_id.encode())
        return SealedSecret(self._key_id, nonce, ciphertext)

    def open(self, sealed: SealedSecret) -> str:
        if sealed.key_id != self._key_id:
            raise KeyError(sealed.key_id)
        try:
            data = self._aead.decrypt(sealed.nonce, sealed.ciphertext, sealed.key_id.encode())
        except InvalidTag:
            raise ValueError("secret failed authentication") from None
        return data.decode()


def secret_box_from_env(env: Mapping[str, str] | None = None) -> AesGcmSecretBox:
    source = os.environ if env is None else env
    value = source.get(MASTER_KEY_ENV)
    if value is None:
        raise MasterKeyError(f"{MASTER_KEY_ENV} is not set: {_EXPECTED}")
    return AesGcmSecretBox(parse_master_key(value))
