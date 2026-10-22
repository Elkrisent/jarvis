"""Encrypted secret storage in Postgres, with atomic master-key rotation (SPEC §11.2)."""

from __future__ import annotations

import logging
from collections import Counter

from jarvis_core.interfaces import SealedSecret
from jarvis_core.secretbox import AesGcmSecretBox
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from jarvis_api.db.ids import new_id
from jarvis_api.db.models import Secret

logger = logging.getLogger(__name__)


class RotationError(Exception):
    """A rotation was refused. Names key IDs and counts only, never key material."""


class PostgresSecretStore:
    """Stores ``SealedSecret`` records in the ``secrets`` table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, sealed: SealedSecret) -> str:
        secret_id = new_id("sec")
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(Secret).values(
                    id=secret_id,
                    key_id=sealed.key_id,
                    nonce=sealed.nonce,
                    ciphertext=sealed.ciphertext,
                )
            )
        return secret_id

    async def get(self, secret_id: str) -> SealedSecret | None:
        query = select(Secret.key_id, Secret.nonce, Secret.ciphertext).where(Secret.id == secret_id)
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            return None
        return SealedSecret(row.key_id, bytes(row.nonce), bytes(row.ciphertext))

    async def rotate(self, old: AesGcmSecretBox, new: AesGcmSecretBox) -> int:
        """Re-seal every record under ``new`` in one transaction; return rows rewritten.

        Rows already under ``new`` are skipped. If any row is sealed under a key that is neither
        ``old`` nor ``new``, nothing is changed and ``RotationError`` is raised.
        """
        async with self._engine.begin() as conn:
            # Concurrent inserts and updates wait, so the set of rows cannot change under us.
            await conn.execute(text("LOCK TABLE secrets IN SHARE ROW EXCLUSIVE MODE"))
            rows = (
                await conn.execute(
                    select(Secret.id, Secret.key_id, Secret.nonce, Secret.ciphertext).order_by(
                        Secret.id
                    )
                )
            ).all()

            unknown = Counter(r.key_id for r in rows if r.key_id not in (old.key_id, new.key_id))
            if unknown:
                total = sum(unknown.values())
                ids = ", ".join(sorted(unknown))
                raise RotationError(
                    f"{total} secret(s) are sealed under unknown key(s) {ids}; "
                    f"expected {old.key_id} or {new.key_id}. Nothing was changed."
                )

            rewritten = 0
            for row in rows:
                if row.key_id == new.key_id:
                    continue
                try:
                    plaintext = old.open(
                        SealedSecret(row.key_id, bytes(row.nonce), bytes(row.ciphertext))
                    )
                except ValueError:
                    raise RotationError(
                        f"secret {row.id} failed authentication under {old.key_id}; "
                        "nothing was changed"
                    ) from None
                sealed = new.seal(plaintext)
                await conn.execute(
                    update(Secret)
                    .where(Secret.id == row.id)
                    .values(key_id=sealed.key_id, nonce=sealed.nonce, ciphertext=sealed.ciphertext)
                )
                rewritten += 1

        logger.info("rotated %d secret(s) from %s to %s", rewritten, old.key_id, new.key_id)
        return rewritten
