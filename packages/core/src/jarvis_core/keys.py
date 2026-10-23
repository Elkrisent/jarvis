"""JARVIS API key generation and hashing (REQ-11.1-6)."""

import hashlib
import secrets
from dataclasses import dataclass

KEY_PREFIX = "jv_live_"
DISPLAY_PREFIX_LEN = 12

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True)
class GeneratedKey:
    full: str
    prefix: str
    hash: str

    def __repr__(self) -> str:
        return f"GeneratedKey(prefix={self.prefix!r})"


def _base62(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    digits: list[str] = []
    while number:
        number, rem = divmod(number, 62)
        digits.append(_ALPHABET[rem])
    return "".join(reversed(digits))


def hash_jarvis_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_jarvis_key() -> GeneratedKey:
    full = KEY_PREFIX + _base62(secrets.token_bytes(32))
    return GeneratedKey(full=full, prefix=full[:DISPLAY_PREFIX_LEN], hash=hash_jarvis_key(full))


def looks_like_jarvis_key(value: str) -> bool:
    return value.startswith("jv_")
