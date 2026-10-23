import asyncio
import json
import logging
import os
import stat
import threading
from pathlib import Path

import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.config import ApiDependency
from jarvis_gateway.config import snapshot_file
from jarvis_gateway.config.snapshot import ApiEntry, KeyEntry, Snapshot
from jarvis_gateway.config.snapshot_file import CoalescingWriter, SnapshotFile

pytestmark = pytest.mark.unit


def _snap(synced_at: float = 10.0, source: str = "postgres") -> Snapshot:
    cfg = ApiDependency.model_validate(
        {"slug": "weather", "name": "Weather", "base_url": "https://api.example.com"}
    )
    return Snapshot.empty("postgres", synced_at).with_changes(
        apis={"a1": ApiEntry("a1", "p1", "weather", 2, cfg)},
        keys={"h1": KeyEntry("k1", "p1", frozenset({"a1"}), False)},
        source="postgres" if source == "postgres" else "file",
    )


def _leftovers(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name != "snap.json"]


@pytest.mark.req("REQ-8-9")
def test_write_then_read_round_trips_as_file_source(tmp_path: Path) -> None:
    f = SnapshotFile(tmp_path / "snap.json")
    f.write(_snap(12.5), written_at=1000.0)
    got = f.read()
    assert got is not None
    assert got.source == "file"
    assert got.synced_at == 12.5
    assert got.api("p1", "weather") is not None
    assert got.key("h1") is not None
    assert "written_at" in json.loads((tmp_path / "snap.json").read_text())
    assert _leftovers(tmp_path) == []


@pytest.mark.req("REQ-8-9")
def test_file_mode_is_0600(tmp_path: Path) -> None:
    SnapshotFile(tmp_path / "snap.json").write(_snap(), written_at=1.0)
    assert stat.S_IMODE((tmp_path / "snap.json").stat().st_mode) == 0o600


@pytest.mark.req("REQ-8-9")
def test_failed_replace_keeps_old_file_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = SnapshotFile(tmp_path / "snap.json")
    f.write(_snap(1.0), written_at=1.0)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        f.write(_snap(2.0), written_at=2.0)
    monkeypatch.undo()
    got = f.read()
    assert got is not None
    assert got.synced_at == 1.0
    assert _leftovers(tmp_path) == []


async def test_write_async_writes(tmp_path: Path) -> None:
    f = SnapshotFile(tmp_path / "snap.json")
    await f.write_async(_snap(3.0), written_at=1.0)
    got = f.read()
    assert got is not None
    assert got.synced_at == 3.0


def _warned(caplog: pytest.LogCaptureFixture, path: Path) -> None:
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert records
    text = " ".join(r.getMessage() for r in records)
    assert str(path) in text
    assert "weather" not in text
    assert "api.example.com" not in text


def _good_dict() -> dict[str, object]:
    return {**_snap().to_json_dict(), "written_at": "2026-01-01T00:00:00+00:00"}


def _bad_config_dict() -> dict[str, object]:
    data = _good_dict()
    apis = data["apis"]
    assert isinstance(apis, list)
    apis[0]["config"] = {"slug": "weather", "base_url": "not a url secret-marker"}
    return data


@pytest.mark.req("REQ-8-9")
@pytest.mark.parametrize(
    "content",
    [
        b"",
        b'{"format": 1, "apis": [',
        b"\xff\xfe\x00bad",
        json.dumps({**_good_dict(), "format": 2}).encode(),
        json.dumps(_bad_config_dict()).encode(),
        b"[1, 2]",
    ],
    ids=["empty", "truncated", "bad-utf8", "format-2", "invalid-config", "not-object"],
)
def test_bad_content_is_absent_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: bytes
) -> None:
    path = tmp_path / "snap.json"
    path.write_bytes(content)
    with caplog.at_level(logging.WARNING):
        assert SnapshotFile(path).read() is None
    _warned(caplog, path)
    assert "secret-marker" not in caplog.text


@pytest.mark.req("REQ-8-9")
def test_missing_file_is_absent_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "snap.json"
    with caplog.at_level(logging.WARNING):
        assert SnapshotFile(path).read() is None
    _warned(caplog, path)


@pytest.mark.req("REQ-8-9")
def test_oversized_file_is_absent_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "snap.json"
    SnapshotFile(path).write(_snap(), written_at=1.0)
    monkeypatch.setattr(snapshot_file, "MAX_SNAPSHOT_BYTES", 10)
    with caplog.at_level(logging.WARNING):
        assert SnapshotFile(path).read() is None
    _warned(caplog, path)


def test_max_snapshot_bytes_is_64_mib() -> None:
    assert snapshot_file.MAX_SNAPSHOT_BYTES == 64 * 1024 * 1024


@pytest.mark.req("REQ-8-9")
def test_sparse_oversized_file_is_absent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "snap.json"
    with path.open("wb") as fh:
        fh.truncate(snapshot_file.MAX_SNAPSHOT_BYTES + 1)
    with caplog.at_level(logging.WARNING):
        assert SnapshotFile(path).read() is None
    _warned(caplog, path)


@pytest.mark.req("REQ-8-9")
def test_directory_at_path_is_absent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "snap.json"
    path.mkdir()
    with caplog.at_level(logging.WARNING):
        assert SnapshotFile(path).read() is None
    _warned(caplog, path)


@pytest.mark.req("REQ-8-9")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_unreadable_file_is_absent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "snap.json"
    f = SnapshotFile(path)
    f.write(_snap(), written_at=1.0)
    path.chmod(0)
    try:
        with caplog.at_level(logging.WARNING):
            assert f.read() is None
    finally:
        path.chmod(0o600)
    _warned(caplog, path)


@pytest.mark.req("REQ-8-9")
def test_symlink_to_regular_file_in_same_directory_is_read(tmp_path: Path) -> None:
    SnapshotFile(tmp_path / "real.json").write(_snap(7.0), written_at=1.0)
    (tmp_path / "snap.json").symlink_to("real.json")
    got = SnapshotFile(tmp_path / "snap.json").read()
    assert got is not None
    assert got.synced_at == 7.0


@pytest.mark.req("REQ-8-9")
def test_symlink_outside_directory_is_absent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    outside = tmp_path / "other"
    outside.mkdir()
    inside = tmp_path / "dir"
    inside.mkdir()
    SnapshotFile(outside / "real.json").write(_snap(), written_at=1.0)
    (inside / "snap.json").symlink_to(outside / "real.json")
    with caplog.at_level(logging.WARNING):
        assert SnapshotFile(inside / "snap.json").read() is None
    _warned(caplog, inside / "snap.json")


@pytest.mark.req("REQ-8-9")
def test_symlink_to_non_regular_or_dangling_is_absent(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "snap.json").symlink_to("sub")
    assert SnapshotFile(tmp_path / "snap.json").read() is None
    (tmp_path / "snap2.json").symlink_to("missing.json")
    assert SnapshotFile(tmp_path / "snap2.json").read() is None


class _SpyFile(SnapshotFile):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.writes: list[Snapshot] = []

    async def write_async(self, snapshot: Snapshot, written_at: float) -> None:
        self.writes.append(snapshot)


async def _until(predicate: object) -> None:
    assert callable(predicate)
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


@pytest.mark.req("REQ-8-9")
async def test_coalescing_ten_submits_one_write_of_the_last(tmp_path: Path) -> None:
    clock = FakeClock()
    spy = _SpyFile(tmp_path / "snap.json")
    writer = CoalescingWriter(spy, clock)
    snaps = [_snap(float(i)) for i in range(10)]
    for s in snaps:
        writer.submit(s)
    await _until(lambda: len(spy.writes) == 1)
    assert spy.writes[0] is snaps[-1]
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(spy.writes) == 1
    clock.advance(1)
    last = _snap(99.0)
    writer.submit(last)
    await _until(lambda: len(spy.writes) == 2)
    assert spy.writes[1] is last
    await writer.aclose()
    assert len(spy.writes) == 2


@pytest.mark.req("REQ-8-9")
async def test_coalescing_submit_within_window_is_deferred_not_dropped(tmp_path: Path) -> None:
    clock = FakeClock()
    spy = _SpyFile(tmp_path / "snap.json")
    writer = CoalescingWriter(spy, clock)
    writer.submit(_snap(1.0))
    await _until(lambda: len(spy.writes) == 1)
    later = _snap(2.0)
    writer.submit(later)
    await writer.aclose()
    assert spy.writes[-1] is later


@pytest.mark.req("REQ-8-9")
async def test_aclose_flushes_pending_submission(tmp_path: Path) -> None:
    spy = _SpyFile(tmp_path / "snap.json")
    writer = CoalescingWriter(spy, FakeClock())
    pending = _snap(5.0)
    writer.submit(pending)
    await writer.aclose()
    assert spy.writes == [pending]
    await writer.aclose()  # idempotent
    assert spy.writes == [pending]


async def test_aclose_without_submit_is_a_noop(tmp_path: Path) -> None:
    spy = _SpyFile(tmp_path / "snap.json")
    await CoalescingWriter(spy, FakeClock()).aclose()
    assert spy.writes == []


@pytest.mark.req("REQ-8-9")
async def test_writer_logs_write_failure_and_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = SnapshotFile(tmp_path / "snap.json")
    f.write(_snap(1.0), written_at=1.0)

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    writer = CoalescingWriter(f, FakeClock())
    with caplog.at_level(logging.ERROR):
        writer.submit(_snap(2.0))
        await writer.aclose()
    monkeypatch.undo()
    assert str(tmp_path / "snap.json") in caplog.text
    got = f.read()
    assert got is not None
    assert got.synced_at == 1.0
    assert _leftovers(tmp_path) == []


@pytest.mark.req("REQ-8-9")
def test_concurrent_writers_never_expose_a_partial_file(tmp_path: Path) -> None:
    path = tmp_path / "snap.json"
    SnapshotFile(path).write(_snap(0.0), written_at=0.0)
    valid = {float(i) for i in range(50)} | {0.0}
    start = threading.Barrier(3)
    done = threading.Event()
    errors: list[BaseException] = []

    def writer(offset: int) -> None:
        f = SnapshotFile(path)
        start.wait()
        try:
            for i in range(25):
                n = offset + i * 2
                f.write(_snap(float(n)), written_at=float(n))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(o,)) for o in (0, 1)]
    for t in threads:
        t.start()
    reader = SnapshotFile(path)
    reads = 0
    start.wait()
    while any(t.is_alive() for t in threads) or reads < 50:
        got = reader.read()
        assert got is not None, "partial or missing file observed"
        assert got.synced_at in valid
        reads += 1
    done.set()
    for t in threads:
        t.join()
    assert not errors
    assert _leftovers(tmp_path) == []


class _ControlledClock:
    """``sleep`` blocks until the test advances time past its deadline."""

    def __init__(self) -> None:
        self._now = 1_000.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self._now + seconds, fut))
        await fut

    def advance(self, seconds: float) -> None:
        self._now += seconds
        for deadline, fut in list(self._sleepers):
            if deadline <= self._now and not fut.done():
                fut.set_result(None)


async def _settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


@pytest.mark.req("REQ-8-9")
async def test_throttle_holds_second_write_until_a_second_passes(tmp_path: Path) -> None:
    clock = _ControlledClock()
    spy = _SpyFile(tmp_path / "snap.json")
    writer = CoalescingWriter(spy, clock)
    writer.submit(_snap(1.0))
    await _until(lambda: len(spy.writes) == 1)
    writer.submit(_snap(2.0))
    latest = _snap(3.0)
    writer.submit(latest)
    await _settle()
    assert len(spy.writes) == 1
    clock.advance(0.5)
    await _settle()
    assert len(spy.writes) == 1
    clock.advance(0.5)
    await _until(lambda: len(spy.writes) == 2)
    assert spy.writes[1] is latest
    await writer.aclose()
    assert len(spy.writes) == 2


class _SlowFile(SnapshotFile):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.writes: list[Snapshot] = []

    async def write_async(self, snapshot: Snapshot, written_at: float) -> None:
        self.started.set()
        await self.release.wait()
        self.writes.append(snapshot)


@pytest.mark.req("REQ-8-9")
async def test_aclose_during_in_flight_write_completes_and_flushes_pending(
    tmp_path: Path,
) -> None:
    slow = _SlowFile(tmp_path / "snap.json")
    writer = CoalescingWriter(slow, FakeClock())
    first, second = _snap(1.0), _snap(2.0)
    writer.submit(first)
    await asyncio.wait_for(slow.started.wait(), 2)
    writer.submit(second)
    closing = asyncio.create_task(writer.aclose())
    await _settle()
    assert not closing.done()  # waits for the in-flight write
    slow.release.set()
    await asyncio.wait_for(closing, 2)
    assert slow.writes == [first, second]


@pytest.mark.req("REQ-8-9")
async def test_aclose_cuts_a_throttle_wait_short_and_flushes(tmp_path: Path) -> None:
    clock = _ControlledClock()
    spy = _SpyFile(tmp_path / "snap.json")
    writer = CoalescingWriter(spy, clock)
    writer.submit(_snap(1.0))
    await _until(lambda: len(spy.writes) == 1)
    pending = _snap(2.0)
    writer.submit(pending)
    await _settle()
    await asyncio.wait_for(writer.aclose(), 2)
    assert spy.writes[-1] is pending
    assert len(spy.writes) == 2


@pytest.mark.req("REQ-8-9")
async def test_cancelling_the_caller_of_aclose_propagates(tmp_path: Path) -> None:
    slow = _SlowFile(tmp_path / "snap.json")
    writer = CoalescingWriter(slow, FakeClock())
    writer.submit(_snap(1.0))
    await asyncio.wait_for(slow.started.wait(), 2)
    closing = asyncio.create_task(writer.aclose())
    await _settle()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    slow.release.set()
    await writer.aclose()


async def test_submit_after_close_is_dropped(tmp_path: Path) -> None:
    spy = _SpyFile(tmp_path / "snap.json")
    writer = CoalescingWriter(spy, FakeClock())
    await writer.aclose()
    writer.submit(_snap())
    await _settle()
    await writer.aclose()
    assert spy.writes == []


@pytest.mark.req("REQ-8-9")
async def test_directory_fsync_failure_is_logged_and_keeps_new_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = SnapshotFile(tmp_path / "snap.json")
    real_fsync = os.fsync
    calls = 0

    def flaky(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:  # the directory fsync follows the file fsync
            raise OSError("dir fsync")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", flaky)
    writer = CoalescingWriter(f, FakeClock())
    with caplog.at_level(logging.ERROR):
        writer.submit(_snap(4.0))
        await writer.aclose()
    monkeypatch.undo()
    assert str(tmp_path / "snap.json") in caplog.text
    got = f.read()
    assert got is not None
    assert got.synced_at == 4.0
    assert _leftovers(tmp_path) == []


@pytest.mark.req("REQ-8-9")
def test_file_fsync_failure_removes_temp_and_keeps_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = SnapshotFile(tmp_path / "snap.json")
    f.write(_snap(1.0), written_at=1.0)

    def boom(_fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="fsync failed"):
        f.write(_snap(2.0), written_at=2.0)
    monkeypatch.undo()
    got = f.read()
    assert got is not None
    assert got.synced_at == 1.0
    assert _leftovers(tmp_path) == []


@pytest.mark.req("REQ-8-9")
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs")
def test_fifo_at_path_is_absent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "snap.json"
    os.mkfifo(path)
    with caplog.at_level(logging.WARNING):
        assert SnapshotFile(path).read() is None
    _warned(caplog, path)


@pytest.mark.req("REQ-8-9")
def test_file_that_grows_after_the_size_check_is_absent_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size read from fstat can be stale (the file is appended to meanwhile): the bytes
    actually read are bounded and checked too."""
    path = tmp_path / "snap.json"
    SnapshotFile(path).write(_snap(), written_at=1.0)
    monkeypatch.setattr(snapshot_file, "MAX_SNAPSHOT_BYTES", 10)
    real_fstat = os.fstat

    class StaleStat:
        def __init__(self, real: os.stat_result) -> None:
            self.st_mode = real.st_mode
            self.st_size = 0

    monkeypatch.setattr(os, "fstat", lambda fd: StaleStat(real_fstat(fd)))
    with caplog.at_level(logging.WARNING):
        assert SnapshotFile(path).read() is None
    _warned(caplog, path)
