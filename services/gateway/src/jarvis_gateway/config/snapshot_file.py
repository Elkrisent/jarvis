"""Local snapshot file: the gateway's fallback config when Postgres is down."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from jarvis_core.clock import Clock

from jarvis_gateway.config.snapshot import Snapshot

log = logging.getLogger(__name__)

MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MIN_WRITE_INTERVAL_S = 1.0


class SnapshotFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, snapshot: Snapshot, *, written_at: float) -> None:
        """Blocking atomic write: temp file in the same directory, fsync, rename.

        If the final directory fsync fails the new file is already in place (it is
        never removed); the error propagates so the caller can log it.
        """
        stamp = datetime.fromtimestamp(written_at, UTC).isoformat()
        payload = json.dumps({**snapshot.to_json_dict(), "written_at": stamp}).encode()
        directory = self.path.parent
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".snapshot-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    async def write_async(self, snapshot: Snapshot, written_at: float) -> None:
        await asyncio.to_thread(self.write, snapshot, written_at=written_at)

    def read(self) -> Snapshot | None:
        """The stored snapshot (``source="file"``), or None with a WARNING.

        Blocking file IO: async callers must run it in a thread.
        """
        try:
            raw = self._read_bytes()
            data = json.loads(raw.decode("utf-8"))
            parsed = Snapshot.from_json_dict(data)
        except _Unusable as exc:
            return self._absent(str(exc))
        except OSError as exc:
            return self._absent(f"cannot read ({exc.strerror or type(exc).__name__})")
        except ValueError as exc:  # JSON, UTF-8 and snapshot errors; no contents in the text
            return self._absent(_safe_reason(exc))
        return Snapshot(parsed.apis, parsed.keys, "file", parsed.synced_at)

    def _read_bytes(self) -> bytes:
        target = self.path
        if target.is_symlink():
            resolved = target.resolve()
            if resolved.parent != target.parent.resolve():
                raise _Unusable("symlink points outside the snapshot directory")
            target = resolved
        fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise _Unusable("not a regular file")
            if info.st_size > MAX_SNAPSHOT_BYTES:
                raise _Unusable("file exceeds the size limit")
            raw = fh.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise _Unusable("file exceeds the size limit")
        if not raw:
            raise _Unusable("file is empty")
        return raw

    def _absent(self, reason: str) -> Snapshot | None:
        log.warning("ignoring snapshot file %s: %s", self.path, reason)
        return None


class _Unusable(Exception):
    pass


def _safe_reason(exc: ValueError) -> str:
    if isinstance(exc, UnicodeDecodeError):
        return "invalid UTF-8"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid JSON"
    return str(exc)  # Snapshot.from_json_dict messages never carry config contents


class CoalescingWriter:
    """Persists the latest submitted snapshot, at most once per second.

    ``aclose`` must be called at shutdown: it lets an in-flight write finish,
    then writes the latest pending snapshot once. ``submit`` after close is
    ignored. Write failures are logged, never raised.
    """

    def __init__(self, file: SnapshotFile, clock: Clock) -> None:
        self._file = file
        self._clock = clock
        self._pending: Snapshot | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._sleeper: asyncio.Task[None] | None = None
        self._last_write: float | None = None
        self._closed = False

    def submit(self, snapshot: Snapshot) -> None:
        if self._closed:
            return
        self._pending = snapshot
        self._wake.set()
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def aclose(self) -> None:
        self._closed = True
        self._wake.set()
        if self._sleeper is not None:
            self._sleeper.cancel()  # cut a throttle wait short; never an in-flight write
        if self._task is not None:
            await asyncio.shield(self._task)  # caller cancellation propagates, writer survives
        await self._flush()

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._closed:
                return
            if self._last_write is not None:
                remaining = _MIN_WRITE_INTERVAL_S - (self._clock.monotonic() - self._last_write)
                if remaining > 0:
                    await self._throttle(remaining)
                    if self._closed:
                        return
            await self._flush()

    async def _throttle(self, seconds: float) -> None:
        sleeper = asyncio.get_running_loop().create_task(self._clock.sleep(seconds))
        self._sleeper = sleeper
        try:
            await asyncio.wait({sleeper})  # a cancelled sleeper does not raise here
        finally:
            sleeper.cancel()
            self._sleeper = None

    async def _flush(self) -> None:
        snapshot, self._pending = self._pending, None
        if snapshot is None:
            return
        try:
            await self._file.write_async(snapshot, self._clock.now())
        except Exception as exc:
            log.error("failed to write snapshot file %s: %s", self._file.path, type(exc).__name__)
        finally:
            self._last_write = self._clock.monotonic()
