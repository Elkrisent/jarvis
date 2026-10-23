"""Request IDs: ULIDs, so they sort by arrival time (SPEC §4.3 step 1)."""

from __future__ import annotations

import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_request_id(now_ms: int | None = None) -> str:
    """48-bit millisecond time and 80 random bits, as 26 Crockford base32 characters."""
    millis = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    value = ((millis & (2**48 - 1)) << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        value, rem = divmod(value, 32)
        chars.append(_CROCKFORD[rem])
    return "".join(reversed(chars))
