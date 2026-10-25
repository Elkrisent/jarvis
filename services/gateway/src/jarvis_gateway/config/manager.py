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


class Subscriber(Protocol):
    def __call__(
        self,
        redis_url: str,
        on_event: Callable[[ConfigEvent], Awaitable[None]],
        on_connected: Callable[[], Awaitable[None]],
        clock: Clock,
        stop: asyncio.Event,
        *,
        on_disconnected: Callable[[], None],
    ) -> Awaitable[None]: ...


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
        snapshot_max_age_s: float | None = None,
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
        self._snapshot_max_age_s = snapshot_max_age_s
        self._subscriber_fn = subscriber
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False
        self._last_sync_ok: float | None = None
        # Failure streaks: full loads remember the reason (logged when it changes); targeted
        # refreshes have their own streak so one never reports the other as recovered.
        self._full_error: str | None = None
        self._refresh_failing = False
        # A failed targeted refresh asks the supervisor for an early full resync.
        self._resync_pending = False
        self._wake = asyncio.Event()
        # While the pub/sub link is down, pushes (incl. revocations) are missed: poll Postgres at
        # the retry interval instead of the resync interval.
        self._subscriber_up = True
        self._last_supervised: float = 0.0

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
        error = await self._load(log_failure=False)
        if error is not None:
            self._full_error = error  # later failures with the same reason stay quiet
            fallback = await asyncio.to_thread(self._file.read)
            if fallback is not None and self._too_old(fallback):
                age = self._clock.now() - fallback.synced_at
                log.error(
                    "the snapshot file %s is too old to serve (%.0f s, limit %.0f s): keys "
                    "revoked since then would still work. Postgres is unavailable (%s); "
                    "retrying every %.0f s",
                    self._file.path,
                    age,
                    self._snapshot_max_age_s,
                    error,
                    RETRY_INTERVAL_S,
                )
            elif fallback is not None:
                self._store.replace(fallback)
                log.warning(
                    "Postgres is unavailable at startup (%s); serving %d APIs and %d keys from "
                    "the snapshot file %s until it is back",
                    error,
                    len(fallback.apis),
                    len(fallback.keys),
                    self._file.path,
                )
            else:
                log.error(
                    "no configuration available: Postgres is unavailable (%s) and there is no "
                    "usable snapshot file at %s; retrying every %.0f s",
                    error,
                    self._file.path,
                    RETRY_INTERVAL_S,
                )
        if self._stop.is_set():
            return  # stop() ran while the first load was in flight: start nothing
        self._last_supervised = self._clock.monotonic()
        self._spawn(self._supervise(), "supervisor")
        if self._redis_url is not None:
            self._spawn(self._subscribe(self._redis_url), "subscriber")

    def _too_old(self, snapshot: Snapshot) -> bool:
        cap = self._snapshot_max_age_s
        return cap is not None and self._clock.now() - snapshot.synced_at > cap

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
            return await self._load(log_failure=True) is None

    async def handle_event(self, event: ConfigEvent) -> None:
        async with self._lock:
            if self._store.current is None:
                return  # nothing loaded yet: the pending full load covers this change
            try:
                await self._refresh(event)
            except Exception as exc:  # any refresh failure is healed by a full resync
                if not self._refresh_failing:
                    # Only the type name unless it is a ConfigLoadError, whose text is safe.
                    reason = str(exc) if isinstance(exc, ConfigLoadError) else type(exc).__name__
                    log.error("config event refresh failed: %s; a full resync will follow", reason)
                self._refresh_failing = True
                self._resync_pending = True
                self._wake.set()
                return
            self._refresh_ok()

    # --- internals ---------------------------------------------------------------------------

    def _spawn(self, coro: Coroutine[None, None, None], name: str) -> None:
        self._tasks.append(asyncio.create_task(coro, name=f"ConfigManager.{name}"))

    async def _supervise(self) -> None:
        while True:
            urgent = self._last_sync_ok is None or self._resync_pending
            delay = RETRY_INTERVAL_S if urgent else self._interval
            if not self._subscriber_up:  # no pushes arriving: poll at least that often
                delay = min(delay, RETRY_INTERVAL_S)
            try:
                await self._pause(delay)
                self._last_supervised = self._clock.monotonic()
                await self.resync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("config resync crashed (%s); will retry", type(exc).__name__)

    async def _pause(self, delay: float) -> None:
        """Sleep ``delay``, or until woken for an early resync (at most one per 5 s)."""
        began = self._clock.monotonic()
        sleeper = asyncio.ensure_future(self._clock.sleep(delay))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
            woken = not sleeper.done()
        finally:
            for task in (sleeper, waker):
                task.cancel()
            await asyncio.gather(sleeper, waker, return_exceptions=True)
        if woken:
            self._wake.clear()
            # Never later than the scheduled resync would have been.
            target = min(self._last_supervised + RETRY_INTERVAL_S, began + delay)
            remaining = target - self._clock.monotonic()
            if remaining > 0:
                await self._clock.sleep(remaining)

    async def _subscribe(self, redis_url: str) -> None:
        async def on_connected() -> None:
            self._subscriber_up = True
            await self.resync()

        def on_disconnected() -> None:
            self._subscriber_up = False
            self._wake.set()

        try:
            await self._subscriber_fn(
                redis_url,
                self.handle_event,
                on_connected,
                self._clock,
                self._stop,
                on_disconnected=on_disconnected,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "config subscriber stopped (%s); relying on the periodic resync",
                type(exc).__name__,
            )

    async def _load(self, *, log_failure: bool) -> str | None:
        """A full load; None on success, else the loader's (DSN-free) error message."""
        now = self._clock.now()
        try:
            fresh, invalid = await self._loader.load_all(now)
        except ConfigLoadError as exc:
            error = str(exc)
            if log_failure and error != self._full_error:
                log.error("configuration resync failed: %s; serving the last snapshot", error)
            if log_failure:
                self._full_error = error
            return error
        previous = self._store.current
        if previous is not None and invalid:
            fresh = fresh.with_changes(apis=_carried(previous, fresh, invalid))
        snapshot = Snapshot(fresh.apis, fresh.keys, "postgres", now)
        self._store.replace(snapshot)
        self._writer.submit(snapshot)
        self._last_sync_ok = now
        self._resync_pending = False
        if self._full_error is not None:
            log.info("configuration resync from Postgres recovered")
        self._full_error = None
        self._refresh_ok()
        return None

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

    def _refresh_ok(self) -> None:
        if self._refresh_failing:
            log.info("config event refresh from Postgres recovered")
        self._refresh_failing = False


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
