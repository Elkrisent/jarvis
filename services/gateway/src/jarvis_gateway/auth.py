"""Gateway authentication: find the JARVIS key in a request and resolve it (SPEC §4.2)."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from jarvis_core.errors import AuthInvalidError
from jarvis_core.keys import hash_jarvis_key, looks_like_jarvis_key
from jarvis_core.request import AuthContext

from jarvis_gateway.config.snapshot import ApiEntry, Snapshot

log = logging.getLogger("jarvis_gateway.auth")

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


@dataclass(frozen=True, repr=False)
class Authenticated:
    auth: AuthContext
    api: ApiEntry

    def __repr__(self) -> str:
        return (
            f"Authenticated(key_id={self.auth.key_id!r}, project_id={self.auth.project_id!r}, "
            f"api_id={self.api.api_id!r})"
        )

    def forward_headers(self, headers: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
        """The request headers without the JARVIS key, whether or not they carried one."""
        drop = {KEY_HEADER, self.api.config.jarvis_key_slot.lower()}
        return [(n, v) for n, v in headers if n.lower() not in drop]


def _denied() -> AuthInvalidError:
    return AuthInvalidError("invalid or missing JARVIS key")


def authenticate(
    snapshot: Snapshot, api_slug: str, headers: Sequence[tuple[str, str]]
) -> Authenticated:
    """Resolve the request's JARVIS key and target API, or raise ``AuthInvalidError``.

    Every failure raises the same error, so a response never says whether the key, the API
    or the scope was wrong. Keys are looked up by their SHA-256 digest, which the caller
    cannot steer, so the lookup leaks nothing useful through timing.
    """
    matches: list[Authenticated] = []
    for api in snapshot.apis_with_slug(api_slug):
        raw = extract_key(headers, api.config.jarvis_key_slot)
        if raw is None:
            continue
        entry = snapshot.key(hash_jarvis_key(raw))
        if entry is None or entry.revoked or entry.project_id != api.project_id:
            continue
        # Empty means every API in the project; a non-empty set must name this API.
        if entry.allowed_api_ids and api.api_id not in entry.allowed_api_ids:
            continue
        allowed = entry.allowed_api_ids or None
        matches.append(
            Authenticated(
                AuthContext(
                    project_id=entry.project_id, key_id=entry.key_id, allowed_api_ids=allowed
                ),
                api,
            )
        )
    if len(matches) > 1:
        log.warning("ambiguous API slug in project %s; denying", matches[0].api.project_id)
    if len(matches) != 1:
        log.debug("auth denied")
        raise _denied()
    return matches[0]
