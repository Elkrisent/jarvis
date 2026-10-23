"""Provider secrets in memory (SPEC §4.5, §11.2).

The config snapshot carries each API's provider secret sealed (ciphertext). Whenever a snapshot is
installed the vault opens them with the master key, so plaintext exists only here, in memory.
Nothing in this module logs, represents or returns a secret except ``secret_for``.
"""

from __future__ import annotations

import logging

from jarvis_core.secretbox import AesGcmSecretBox

from jarvis_gateway.config.snapshot import ApiEntry, Snapshot

log = logging.getLogger(__name__)


class CredentialUnavailable(Exception):
    """The API needs a provider secret and none could be opened (wrong master key, or missing)."""


class CredentialVault:
    def __init__(self, box: AesGcmSecretBox) -> None:
        self._box = box
        self._plain: dict[tuple[str, str], str] = {}
        self._reported: set[tuple[str, str]] = set()

    def __repr__(self) -> str:
        return "CredentialVault()"

    __str__ = __repr__

    def refresh(self, snapshot: Snapshot) -> None:
        """Open every sealed secret of ``snapshot``; secrets of APIs no longer present are dropped.

        A record that cannot be opened (for example after a master-key rotation the gateway has
        not been restarted for) is logged once with the API id and key id only.
        """
        plain: dict[tuple[str, str], str] = {}
        failed: set[tuple[str, str]] = set()
        for api in snapshot.apis.values():
            ref = api.config.credential.secret_ref
            if api.sealed is None or ref is None:
                continue
            try:
                plain[(api.api_id, ref)] = self._box.open(api.sealed, context=ref)
            except (KeyError, ValueError):
                failed.add((api.api_id, api.sealed.key_id))
        for api_id, key_id in failed - self._reported:
            log.warning(
                "cannot open the provider secret of API %s (sealed under key %s): requests that "
                "need it fail until the gateway runs with the matching JARVIS_MASTER_KEY",
                api_id,
                key_id,
            )
        self._reported = failed
        self._plain = plain  # one assignment: readers see the old or the new set, never a mix

    def secret_for(self, api: ApiEntry) -> str | None:
        """The API's provider secret; ``None`` if its mode injects nothing."""
        credential = api.config.credential
        if credential.mode == "none":
            return None
        secret = None
        if credential.secret_ref is not None:
            secret = self._plain.get((api.api_id, credential.secret_ref))
        if secret is None:
            raise CredentialUnavailable
        return secret
