"""Gateway authentication: find the JARVIS key in a request and resolve it (SPEC §4.2)."""

from __future__ import annotations

import re
from collections.abc import Sequence

from jarvis_core.keys import looks_like_jarvis_key

KEY_HEADER = "x-jarvis-key"
MAX_KEY_LENGTH = 256
_BEARER = re.compile(r"^bearer[ \t]+", re.IGNORECASE)


def extract_key(headers: Sequence[tuple[str, str]], slot: str | None) -> str | None:
    """Return the candidate key, or ``None`` when the request carries no usable one.

    A non-blank ``X-Jarvis-Key`` is the only candidate, even if it is invalid. Repeated
    ``X-Jarvis-Key`` headers are ambiguous and give ``None``. Otherwise the API's credential
    slot is read, with a leading ``Bearer`` removed, and counts only if it starts with ``jv_``.
    """
    present = [value.strip(" \t") for name, value in headers if name.lower() == KEY_HEADER]
    if len(present) > 1:
        return None
    if present and present[0]:
        return _bounded(present[0])
    if slot is None:
        return None
    slot = slot.lower()
    values = [value for name, value in headers if name.lower() == slot]
    if len(values) != 1:
        return None
    candidate = _BEARER.sub("", values[0].strip(" \t"), count=1)
    if not looks_like_jarvis_key(candidate):
        return None
    return _bounded(candidate)


def _bounded(value: str) -> str | None:
    return value if len(value) <= MAX_KEY_LENGTH else None
