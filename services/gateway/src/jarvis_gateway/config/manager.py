"""Keeps the gateway's config snapshot current: startup, targeted refresh, resync, subscriber.

All Postgres reads that change the snapshot (full loads and event refreshes) run one at a time
under one lock, so events are applied strictly in arrival order and a refresh never interleaves
with a resync. Background tasks never end the process: failures are logged (type names and
loader messages only, never payloads, URLs or config) and the loop continues.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from jarvis_core.clock import Clock

from jarvis_gateway.config import events
from jarvis_gateway.config.events import ApiChanged, ConfigEvent, Invalidated
from jarvis_gateway.config.postgres import ConfigLoadError, InvalidApi, ProjectRows
from jarvis_gateway.config.snapshot import ApiEntry, KeyEntry, Snapshot
from jarvis_gateway.config.store import ConfigStore

log = logging.getLogger(__name__)

RETRY_INTERVAL_S = 5.0


class Loader(Protocol):
    async def load_all(self, now: float) -> tuple[Snapshot, frozenset[str]]: ...

    async def load_api(self, api_id: str) -> ApiEntry | InvalidApi | None: ...

    async def load_key(self, key_id: str) -> tuple[str, KeyEntry] | None: ...

    async def load_project(self, project_id: str) -> ProjectRows: ...

    async def close(self) -> None: ...


class SnapshotSource(Protocol):
    @property
    def path(self) -> object: ...

    def read(self) -> Snapshot | None: ...


class SnapshotWriter(Protocol):
    def submit(self, snapshot: Snapshot) -> None: ...

    async def aclose(self) -> None: ...


Subscriber = Callable[
    [
        str,
        Callable[[ConfigEvent], Awaitable[None]],
        Callable[[], Awaitable[None]],
        Clock,
        asyncio.Event,
    ],
    Awaitable[None],
]


@dataclass(frozen=True)
class ConfigStatus:
    source: Literal["postgres", "file", "none"]
    apis: int
    keys: int
    synced_at: float | None  # when the served snapshot was last fully loaded from Postgres
    last_sync_ok: float | None  # when this process last completed a full load (None: never)


class ConfigManager:
    def __init__(
        self,
        loader: Loader,
        snapshot_file: SnapshotSource,
        writer: SnapshotWriter,
        store: ConfigStore,
        clock: Clock,
        *,
        resync_interval_s: float,
        redis_url: str | None,
        subscriber: Subscriber = events.subscribe,
    ) -> None:
        if not resync_interval_s > 0:
            raise ValueError("resync_interval_s must be positive")
        self._loader = loader
        self._file = snapshot_file
        self._writer = writer
        self._store = store
        self._clock = clock
        self._interval = resync_interval_s
        self._redis_url = redis_url
        self._subscriber_fn = subscriber
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False
        self._last_sync_ok: float | None = None
        self._failing = False

    @property
    def store(self) -> ConfigStore:
        return self._store

    def status(self) -> ConfigStatus:
        snap = self._store.current
        if snap is None:
            return ConfigStatus("none", 0, 0, None, self._last_sync_ok)
        return ConfigStatus(
            snap.source, len(snap.apis), len(snap.keys), snap.synced_at, self._last_sync_ok
        )

    async def start(self) -> None:
        """One Postgres attempt, else the snapshot file, else nothing; then go to the background.

        Returns once the first attempt finished. Without a Postgres load, loading is retried
        every 5 s until it succeeds.
        """
        if self._started:
            raise RuntimeError("config manager already started")
        self._started = True
        if not await self._load(log_failure=False):
            self._failing = True
            fallback = await asyncio.to_thread(self._file.read)
            if fallback is not None:
                self._store.replace(fallback)
                log.warning(
                    "Postgres is unreachable at startup; serving %d APIs and %d keys from the "
                    "snapshot file %s until it is back",
                    len(fallback.apis),
                    len(fallback.keys),
                    self._file.path,
                )
            else:
                log.error(
                    "no configuration available: Postgres is unreachable and there is no usable "
                    "snapshot file at %s; retrying every %.0f s",
                    self._file.path,
                    RETRY_INTERVAL_S,
                )
        self._spawn(self._supervise(), "supervisor")
        if self._redis_url is not None:
            self._spawn(self._subscribe(self._redis_url), "subscriber")

    async def stop(self) -> None:
        """Cancel and await the background tasks, flush the snapshot file, close the loader."""
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            try:
                await self._writer.aclose()
            finally:
                await self._loader.close()

    async def resync(self) -> bool:
        """Full reload from Postgres. False (old snapshot kept) when Postgres cannot be read."""
        async with self._lock:
            return await self._load(log_failure=True)

    async def handle_event(self, event: ConfigEvent) -> None:
        async with self._lock:
            if self._store.current is None:
                return  # nothing loaded yet: the pending full load covers this change
            try:
                await self._refresh(event)
            except ConfigLoadError as exc:
                self._failed(exc)
                return
            self._succeeded()

    # --- internals ---------------------------------------------------------------------------

    def _spawn(self, coro: Coroutine[None, None, None], name: str) -> None:
        self._tasks.append(asyncio.create_task(coro, name=f"ConfigManager.{name}"))

    async def _supervise(self) -> None:
        while True:
            delay = RETRY_INTERVAL_S if self._last_sync_ok is None else self._interval
            try:
                await self._clock.sleep(delay)
                await self.resync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("config resync crashed (%s); will retry", type(exc).__name__)

    async def _subscribe(self, redis_url: str) -> None:
        async def on_connected() -> None:
            await self.resync()

        try:
            await self._subscriber_fn(
                redis_url, self.handle_event, on_connected, self._clock, self._stop
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "config subscriber stopped (%s); relying on the periodic resync",
                type(exc).__name__,
            )

    async def _load(self, *, log_failure: bool) -> bool:
        now = self._clock.now()
        try:
            fresh, invalid = await self._loader.load_all(now)
        except ConfigLoadError as exc:
            if log_failure:
                self._failed(exc)
            return False
        previous = self._store.current
        if previous is not None and invalid:
            fresh = fresh.with_changes(apis=_carried(previous, fresh, invalid))
        snapshot = Snapshot(fresh.apis, fresh.keys, "postgres", now)
        self._store.replace(snapshot)
        self._writer.submit(snapshot)
        self._last_sync_ok = now
        self._succeeded()
        return True

    async def _refresh(self, event: ConfigEvent) -> None:
        current = self._store.current
        assert current is not None
        match event:
            case ApiChanged(api_id=api_id, version=version):
                held = current.api_by_id(api_id)
                if held is not None and held.version >= version:
                    return  # stale or duplicate: already serving this version or newer
                self._apply_api(api_id, await self._loader.load_api(api_id))
            case Invalidated(kind="api", id=api_id):
                self._apply_api(api_id, await self._loader.load_api(api_id))
            case Invalidated(kind="key", id=key_id):
                found = await self._loader.load_key(key_id)
                self._apply(lambda s: _with_key(s, key_id, found))
            case Invalidated(kind="project", id=project_id):
                rows = await self._loader.load_project(project_id)
                self._apply(lambda s: _with_project(s, project_id, rows))

    def _apply_api(self, api_id: str, loaded: ApiEntry | InvalidApi | None) -> None:
        if isinstance(loaded, InvalidApi):
            return  # the row exists but cannot be served: keep the previous entry (if any)
        changes: Mapping[str, ApiEntry | None] = {api_id: loaded}
        self._apply(lambda s: s.with_changes(apis=changes))

    def _apply(self, change: Callable[[Snapshot], Snapshot]) -> None:
        """Swap in ``change(current)`` and persist it, unless nothing changed."""

        def unless_unchanged(current: Snapshot) -> Snapshot:
            new = change(current)
            return current if new.apis == current.apis and new.keys == current.keys else new

        before = self._store.current
        after = self._store.update(unless_unchanged)
        if after is not before:
            self._writer.submit(after)

    def _failed(self, exc: ConfigLoadError) -> None:
        if not self._failing:
            log.error("configuration refresh failed: %s; serving the last snapshot", exc)
        self._failing = True

    def _succeeded(self) -> None:
        if self._failing:
            log.info("configuration refresh from Postgres recovered")
        self._failing = False


def _carried(previous: Snapshot, fresh: Snapshot, invalid: frozenset[str]) -> dict[str, ApiEntry]:
    """Previous entries for rows now invalid, unless a valid fresh API holds their slug."""
    carried: dict[str, ApiEntry] = {}
    for api_id in invalid:
        old = previous.api_by_id(api_id)
        if old is not None and fresh.api(old.project_id, old.slug) is None:
            carried[api_id] = old
    return carried


def _with_key(snap: Snapshot, key_id: str, found: tuple[str, KeyEntry] | None) -> Snapshot:
    changes: dict[str, KeyEntry | None] = {
        h: None for h, k in snap.keys.items() if k.key_id == key_id
    }
    if found is not None:
        changes[found[0]] = found[1]
    return snap.with_changes(keys=changes)


def _with_project(snap: Snapshot, project_id: str, rows: ProjectRows) -> Snapshot:
    """The project's rows replace its entries; a now-invalid row keeps its previous entry."""
    apis: dict[str, ApiEntry | None] = {a.api_id: a for a in rows.apis}
    slugs = {a.slug for a in rows.apis}
    for api_id in rows.invalid_api_ids:
        old = snap.api_by_id(api_id)
        if old is not None and old.project_id == project_id and old.slug not in slugs:
            apis[api_id] = old
    return snap.with_changes(apis=apis, keys=dict(rows.keys), remove_project=project_id)
