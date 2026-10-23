"""Read-only loader of API policies and JARVIS key hashes from Postgres.

The gateway only ever issues ``SELECT`` over ``api_dependencies``, ``api_policy_versions`` and
``jarvis_keys``. Errors never carry the DSN, a host or a password: only the failure's type name.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit

import asyncpg
from jarvis_core.config import ApiDependency
from pydantic import ValidationError

from jarvis_gateway.config.snapshot import ApiEntry, KeyEntry, Snapshot

logger = logging.getLogger(__name__)

_COMMAND_TIMEOUT_S = 10.0
_MAX_CONNECTIONS = 2
_FAILURES = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)
# DSN query options this project plausibly uses and asyncpg understands. ``connect_timeout`` is
# accepted but dropped (the loader's own timeout governs). ``options`` is deliberately absent: it
# can set arbitrary server parameters.
_DSN_OPTIONS = frozenset(
    {
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
        "application_name",
        "target_session_attrs",
        "connect_timeout",
    }
)

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


@dataclass(frozen=True)
class InvalidApi:
    """An API row that exists but cannot be served (no current version, or an invalid policy).

    Distinct from ``None`` ("the row is gone") so the caller can keep the previous entry.
    """

    api_id: str


class ProjectRows(NamedTuple):
    apis: list[ApiEntry]
    keys: list[tuple[str, KeyEntry]]
    invalid_api_ids: frozenset[str]


class PostgresLoader:
    def __init__(self, dsn: str, *, connect_timeout_s: float = 5.0) -> None:
        self._dsn = dsn
        self._timeout = connect_timeout_s
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()
        self._dsn_error: str | None = None
        try:
            self._dsn = _asyncpg_dsn(dsn)
        except ConfigLoadError as exc:  # raised on first use, so construction never fails
            self._dsn_error = str(exc)

    def __repr__(self) -> str:
        return "PostgresLoader()"

    async def load_all(self, now: float) -> tuple[Snapshot, frozenset[str]]:
        """The whole config plus the ids of API rows that were skipped as invalid."""

        async def run(conn: asyncpg.Connection) -> tuple[Snapshot, frozenset[str]]:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                api_rows = await conn.fetch(_API_SELECT)
                key_rows = await conn.fetch(_KEY_SELECT)
            apis, invalid = _split(api_rows)
            keys = dict(map(_key_entry, key_rows))
            return Snapshot({a.api_id: a for a in apis}, keys, "postgres", now), invalid

        return await self._run(run)

    async def load_api(self, api_id: str) -> ApiEntry | InvalidApi | None:
        """``None`` means the row is gone; ``InvalidApi`` means it exists but is unusable."""

        async def run(conn: asyncpg.Connection) -> ApiEntry | InvalidApi | None:
            row = await conn.fetchrow(f"{_API_SELECT} WHERE a.id = $1", api_id)
            return None if row is None else _api_entry(row)

        return await self._run(run)

    async def load_key(self, key_id: str) -> tuple[str, KeyEntry] | None:
        async def run(conn: asyncpg.Connection) -> tuple[str, KeyEntry] | None:
            row = await conn.fetchrow(f"{_KEY_SELECT} WHERE id = $1", key_id)
            return None if row is None else _key_entry(row)

        return await self._run(run)

    async def load_project(self, project_id: str) -> ProjectRows:
        async def run(conn: asyncpg.Connection) -> ProjectRows:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                api_rows = await conn.fetch(f"{_API_SELECT} WHERE a.project_id = $1", project_id)
                key_rows = await conn.fetch(f"{_KEY_SELECT} WHERE project_id = $1", project_id)
            apis, invalid = _split(api_rows)
            return ProjectRows(apis, [_key_entry(r) for r in key_rows], invalid)

        return await self._run(run)

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await _discard(pool)

    async def _run[T](self, fn: Callable[[asyncpg.Connection], Awaitable[T]]) -> T:
        if self._dsn_error is not None:
            raise ConfigLoadError(self._dsn_error)
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
                try:
                    self._pool = await asyncpg.create_pool(
                        self._dsn,
                        min_size=0,
                        max_size=_MAX_CONNECTIONS,
                        timeout=self._timeout,
                        command_timeout=_COMMAND_TIMEOUT_S,
                    )
                except ValueError:
                    raise ConfigLoadError("invalid database DSN") from None
            return self._pool

    async def _reset(self) -> None:
        # Closes a pool that other in-flight calls may still be using; those then fail with a
        # ConfigLoadError too. The config manager serializes calls, so this does not happen there.
        async with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            await _discard(pool)


def _asyncpg_dsn(dsn: str) -> str:
    """Check the DSN's query options and drop libpq's ``connect_timeout``.

    asyncpg would send an unknown option to the server as a setting, so anything outside the
    allowlist is rejected by name (never by value). ``connect_timeout`` is dropped because the
    loader's own ``connect_timeout_s`` governs. ``sslmode`` is understood by asyncpg as is.
    """
    try:
        parts = urlsplit(dsn)
        query = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return dsn  # left for asyncpg to reject; the error never echoes it
    for name, _ in query:
        if name not in _DSN_OPTIONS:
            raise ConfigLoadError(f"unsupported DSN option: {name}")
    kept = [(k, v) for k, v in query if k != "connect_timeout"]
    if len(kept) == len(query):
        return dsn
    return parts._replace(query=urlencode(kept)).geturl()


async def _discard(pool: asyncpg.Pool) -> None:
    try:
        await asyncio.wait_for(pool.close(), timeout=2.0)
    except _FAILURES:
        pool.terminate()


def _split(rows: list[asyncpg.Record]) -> tuple[list[ApiEntry], frozenset[str]]:
    apis: list[ApiEntry] = []
    invalid: set[str] = set()
    for row in rows:
        entry = _api_entry(row)
        if isinstance(entry, InvalidApi):
            invalid.add(entry.api_id)
        else:
            apis.append(entry)
    return apis, frozenset(invalid)


def _api_entry(row: asyncpg.Record) -> ApiEntry | InvalidApi:
    api_id: str = row["id"]
    raw = row["config_json"]
    if raw is None or row["version"] is None:
        logger.error("skipping API %s: it has no current policy version", api_id)
        return InvalidApi(api_id)
    try:
        config = ApiDependency.model_validate(json.loads(raw) if isinstance(raw, str) else raw)
    except (ValidationError, ValueError):
        # Neither the pydantic detail nor the JSON is logged: they echo config values.
        logger.error("skipping API %s: its current policy is invalid", api_id)
        return InvalidApi(api_id)
    return ApiEntry(api_id, row["project_id"], row["slug"], row["version"], config)


def _key_entry(row: asyncpg.Record) -> tuple[str, KeyEntry]:
    allowed = frozenset(row["allowed_api_ids"] or ())
    return row["hash"], KeyEntry(row["id"], row["project_id"], allowed, bool(row["revoked"]))
