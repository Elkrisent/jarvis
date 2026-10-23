"""Read-only loader of API policies and JARVIS key hashes from Postgres.

The gateway only ever issues ``SELECT`` over ``api_dependencies``, ``api_policy_versions`` and
``jarvis_keys``. Errors never carry the DSN, a host or a password: only the failure's type name.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit

import asyncpg
from jarvis_core.config import ApiDependency
from pydantic import ValidationError

from jarvis_gateway.config.snapshot import ApiEntry, KeyEntry, Snapshot

logger = logging.getLogger(__name__)

_COMMAND_TIMEOUT_S = 10.0
_MAX_CONNECTIONS = 2
_FAILURES = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError, ValueError)

_API_SELECT = (
    "SELECT a.id, a.project_id, a.slug, v.version, v.config_json"
    " FROM api_dependencies a"
    " LEFT JOIN api_policy_versions v ON v.id = a.current_version_id"
)
_KEY_SELECT = (
    "SELECT id, project_id, hash, allowed_api_ids, (revoked_at IS NOT NULL) AS revoked"
    " FROM jarvis_keys"
)


class ConfigLoadError(Exception):
    """Postgres could not be read. The message never contains the DSN or any credential."""


class PostgresLoader:
    def __init__(self, dsn: str, *, connect_timeout_s: float = 5.0) -> None:
        self._dsn = _asyncpg_dsn(dsn)
        self._timeout = connect_timeout_s
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "PostgresLoader()"

    async def load_all(self, now: float) -> Snapshot:
        async def run(conn: asyncpg.Connection) -> Snapshot:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                api_rows = await conn.fetch(_API_SELECT)
                key_rows = await conn.fetch(_KEY_SELECT)
            apis = {e.api_id: e for e in map(_api_entry, api_rows) if e is not None}
            keys = dict(map(_key_entry, key_rows))
            return Snapshot(apis, keys, "postgres", now)

        return await self._run(run)

    async def load_api(self, api_id: str) -> ApiEntry | None:
        async def run(conn: asyncpg.Connection) -> ApiEntry | None:
            row = await conn.fetchrow(f"{_API_SELECT} WHERE a.id = $1", api_id)
            return None if row is None else _api_entry(row)

        return await self._run(run)

    async def load_key(self, key_id: str) -> tuple[str, KeyEntry] | None:
        async def run(conn: asyncpg.Connection) -> tuple[str, KeyEntry] | None:
            row = await conn.fetchrow(f"{_KEY_SELECT} WHERE id = $1", key_id)
            return None if row is None else _key_entry(row)

        return await self._run(run)

    async def load_project(
        self, project_id: str
    ) -> tuple[list[ApiEntry], list[tuple[str, KeyEntry]]]:
        async def run(
            conn: asyncpg.Connection,
        ) -> tuple[list[ApiEntry], list[tuple[str, KeyEntry]]]:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                api_rows = await conn.fetch(f"{_API_SELECT} WHERE a.project_id = $1", project_id)
                key_rows = await conn.fetch(f"{_KEY_SELECT} WHERE project_id = $1", project_id)
            apis = [e for e in map(_api_entry, api_rows) if e is not None]
            return apis, [_key_entry(r) for r in key_rows]

        return await self._run(run)

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await _discard(pool)

    async def _run[T](self, fn: Callable[[asyncpg.Connection], Awaitable[T]]) -> T:
        try:
            pool = await self._get_pool()
            async with pool.acquire(timeout=self._timeout) as conn:
                return await fn(conn)
        except _FAILURES as exc:
            await self._reset()
            # The cause is dropped on purpose: asyncpg errors can echo host, user or DSN pieces.
            raise ConfigLoadError(
                f"could not read configuration from Postgres ({type(exc).__name__})"
            ) from None

    async def _get_pool(self) -> asyncpg.Pool:
        async with self._lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=0,
                    max_size=_MAX_CONNECTIONS,
                    timeout=self._timeout,
                    command_timeout=_COMMAND_TIMEOUT_S,
                )
            return self._pool

    async def _reset(self) -> None:
        async with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            await _discard(pool)


def _asyncpg_dsn(dsn: str) -> str:
    """Drop libpq's ``connect_timeout``, which asyncpg would send to the server as a setting.

    The loader's own ``connect_timeout_s`` governs. ``sslmode`` is understood by asyncpg as is.
    """
    try:
        parts = urlsplit(dsn)
        query = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return dsn  # left for asyncpg to reject; the error never echoes it
    kept = [(k, v) for k, v in query if k != "connect_timeout"]
    if len(kept) == len(query):
        return dsn
    return parts._replace(query=urlencode(kept)).geturl()


async def _discard(pool: asyncpg.Pool) -> None:
    try:
        await asyncio.wait_for(pool.close(), timeout=2.0)
    except _FAILURES:
        pool.terminate()


def _api_entry(row: asyncpg.Record) -> ApiEntry | None:
    api_id: str = row["id"]
    raw = row["config_json"]
    if raw is None or row["version"] is None:
        logger.error("skipping API %s: it has no current policy version", api_id)
        return None
    try:
        config = ApiDependency.model_validate(json.loads(raw) if isinstance(raw, str) else raw)
    except (ValidationError, ValueError):
        # Neither the pydantic detail nor the JSON is logged: they echo config values.
        logger.error("skipping API %s: its current policy is invalid", api_id)
        return None
    return ApiEntry(api_id, row["project_id"], row["slug"], row["version"], config)


def _key_entry(row: asyncpg.Record) -> tuple[str, KeyEntry]:
    allowed = frozenset(row["allowed_api_ids"] or ())
    return row["hash"], KeyEntry(row["id"], row["project_id"], allowed, bool(row["revoked"]))
