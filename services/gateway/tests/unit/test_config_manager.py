"""ConfigManager with a fake loader and a stepping fake clock (no Postgres, no Redis)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from jarvis_core.clock import Clock, FakeClock
from jarvis_core.config import ApiDependency
from jarvis_gateway.config.events import ApiChanged, ConfigEvent, Invalidated
from jarvis_gateway.config.manager import RETRY_INTERVAL_S, ConfigManager, ConfigStatus
from jarvis_gateway.config.postgres import ConfigLoadError, InvalidApi, PostgresLoader, ProjectRows
from jarvis_gateway.config.snapshot import ApiEntry, KeyEntry, Snapshot
from jarvis_gateway.config.snapshot_file import CoalescingWriter, SnapshotFile
from jarvis_gateway.config.store import ConfigStore

pytestmark = pytest.mark.unit

LOGGER = "jarvis_gateway.config.manager"
INTERVAL = 60.0
LOAD_ERROR = "could not read configuration from Postgres (OSError)"


class SteppingClock(FakeClock):
    """``sleep`` blocks until the test advances time past its deadline."""

    def __init__(self) -> None:
        super().__init__()
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    async def sleep(self, seconds: float) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.monotonic() + max(seconds, 0.0), fut))
        try:
            await fut
        finally:
            self._sleepers = [(d, f) for d, f in self._sleepers if f is not fut]

    async def step(self, seconds: float) -> None:
        target = self.monotonic() + seconds
        while True:
            await settle()
            due = sorted(
                ((d, f) for d, f in self._sleepers if d <= target and not f.done()),
                key=lambda pair: pair[0],
            )
            if not due:
                break
            deadline, fut = due[0]
            if deadline > self.monotonic():
                self.advance(deadline - self.monotonic())
            fut.set_result(None)
        if target > self.monotonic():
            self.advance(target - self.monotonic())
        await settle()


async def settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


def _api(api_id: str, version: int = 1, project: str = "p1", slug: str | None = None) -> ApiEntry:
    slug = slug or api_id
    config = ApiDependency.model_validate(
        {"slug": slug, "name": f"{slug} v{version}", "base_url": "https://api.example.com"}
    )
    return ApiEntry(api_id, project, slug, version, config)


def _key(key_id: str, project: str = "p1", revoked: bool = False) -> KeyEntry:
    return KeyEntry(key_id, project, frozenset({"a1"}), revoked)


class FakeLoader:
    def __init__(self) -> None:
        self.apis: dict[str, ApiEntry | InvalidApi] = {}
        self.keys: dict[str, KeyEntry] = {}
        self.fail = False
        self.calls: list[str] = []
        self.closed = False
        self.gate: asyncio.Event | None = None
        self.all_gate: asyncio.Event | None = None
        self.in_flight = 0
        self.max_in_flight = 0

    async def _enter(self, what: str) -> None:
        self.calls.append(what)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(0)
        finally:
            self.in_flight -= 1
        if self.fail:
            raise ConfigLoadError(LOAD_ERROR)

    async def load_all(self, now: float) -> tuple[Snapshot, frozenset[str]]:
        # Rows are read first, then ``all_gate`` holds the result in flight (a slow resync).
        valid = {k: v for k, v in self.apis.items() if isinstance(v, ApiEntry)}
        invalid = frozenset(k for k, v in self.apis.items() if isinstance(v, InvalidApi))
        keys = dict(self.keys)
        if self.all_gate is not None:
            self.calls.append("all:read")
            await self.all_gate.wait()
        await self._enter("all")
        return Snapshot(valid, keys, "postgres", now), invalid

    async def load_api(self, api_id: str) -> ApiEntry | InvalidApi | None:
        await self._enter(f"api:{api_id}")
        return self.apis.get(api_id)

    async def load_key(self, key_id: str) -> tuple[str, KeyEntry] | None:
        await self._enter(f"key:{key_id}")
        for h, k in self.keys.items():
            if k.key_id == key_id:
                return h, k
        return None

    async def load_project(self, project_id: str) -> ProjectRows:
        await self._enter(f"project:{project_id}")
        apis = [v for v in self.apis.values() if isinstance(v, ApiEntry)]
        # InvalidApi carries no project: report them all; the manager must filter.
        invalid = frozenset(k for k, v in self.apis.items() if isinstance(v, InvalidApi))
        return ProjectRows(
            [a for a in apis if a.project_id == project_id],
            [(h, k) for h, k in self.keys.items() if k.project_id == project_id],
            invalid,
        )

    async def close(self) -> None:
        self.closed = True


class FakeWriter:
    def __init__(self) -> None:
        self.submitted: list[Snapshot] = []
        self.closed = False

    def submit(self, snapshot: Snapshot) -> None:
        self.submitted.append(snapshot)

    async def aclose(self) -> None:
        self.closed = True


class Harness:
    def __init__(
        self, tmp_path: Path, *, real_writer: bool = False, snapshot_max_age_s: float | None = None
    ) -> None:
        self.clock = SteppingClock()
        self.loader = FakeLoader()
        self.file = SnapshotFile(tmp_path / "snapshot.json")
        self.store = ConfigStore()
        self.fake_writer = FakeWriter()
        self.writer: FakeWriter | CoalescingWriter = (
            CoalescingWriter(self.file, self.clock) if real_writer else self.fake_writer
        )
        self.manager = ConfigManager(
            self.loader,
            self.file,
            self.writer,
            self.store,
            self.clock,
            resync_interval_s=INTERVAL,
            redis_url=None,
            snapshot_max_age_s=snapshot_max_age_s,
        )

    @property
    def snap(self) -> Snapshot:
        current = self.store.current
        assert current is not None
        return current


@pytest.fixture
async def h(tmp_path: Path) -> Harness:
    harness = Harness(tmp_path)
    harness.loader.apis = {"a1": _api("a1", 4), "a2": _api("a2", 1, "p2")}
    harness.loader.keys = {"h1": _key("k1"), "h2": _key("k2", "p2")}
    return harness


async def _started(h: Harness) -> Harness:
    await h.manager.start()
    return h


# --- startup --------------------------------------------------------------------------------


@pytest.mark.req("REQ-5.2-4")
async def test_startup_loads_from_postgres_and_writes_the_file(tmp_path: Path) -> None:
    h = Harness(tmp_path, real_writer=True)
    h.loader.apis = {"a1": _api("a1", 2)}
    h.loader.keys = {"h1": _key("k1")}
    await h.manager.start()
    assert h.snap.source == "postgres"
    assert h.snap.synced_at == h.clock.now()
    assert h.snap.api_by_id("a1") == _api("a1", 2)
    assert h.manager.store is h.store
    await h.manager.stop()
    written = json.loads(h.file.path.read_text())
    assert [a["api_id"] for a in written["apis"]] == ["a1"]
    assert [k["key_hash"] for k in written["keys"]] == ["h1"]
    assert h.loader.closed


@pytest.mark.req("REQ-8-2")
async def test_startup_falls_back_to_the_file_then_switches_to_postgres(tmp_path: Path) -> None:
    h = Harness(tmp_path, real_writer=True)
    h.file.write(Snapshot({"old": _api("old", 7)}, {}, "postgres", 5.0), written_at=5.0)
    h.loader.fail = True
    await h.manager.start()
    assert h.snap.source == "file"
    assert set(h.snap.apis) == {"old"}
    assert h.manager.status().source == "file"

    h.loader.fail = False
    h.loader.apis = {"new": _api("new")}
    await h.clock.step(5.0)
    assert h.snap.source == "postgres"
    assert set(h.snap.apis) == {"new"}
    await h.manager.stop()
    written = json.loads(h.file.path.read_text())
    assert [a["api_id"] for a in written["apis"]] == ["new"]


async def test_startup_from_the_file_does_not_rewrite_it(h: Harness) -> None:
    h.file.write(Snapshot({}, {}, "postgres", 5.0), written_at=5.0)
    h.loader.fail = True
    await h.manager.start()
    assert h.fake_writer.submitted == []
    await h.manager.stop()


CAP = 3600.0


def _file_aged(h: Harness, age_s: float) -> None:
    h.file.write(
        Snapshot({"old": _api("old", 7)}, {}, "postgres", h.clock.now() - age_s),
        written_at=h.clock.now() - age_s,
    )


@pytest.mark.req("REQ-4.2-6", "REQ-8-2")
@pytest.mark.parametrize("age_s", [CAP + 1, 10 * CAP])
async def test_a_snapshot_file_older_than_the_cap_is_not_served(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, age_s: float
) -> None:
    h = Harness(tmp_path, snapshot_max_age_s=CAP)
    _file_aged(h, age_s)
    h.loader.fail = True
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await h.manager.start()
    assert h.store.current is None
    assert h.manager.status() == ConfigStatus("none", 0, 0, None, None)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "too old" in errors[0]
    assert str(h.file.path) in errors[0]

    h.loader.fail = False
    h.loader.apis = {"new": _api("new")}
    await h.clock.step(5.0)
    assert h.snap.source == "postgres"
    assert set(h.snap.apis) == {"new"}
    await h.manager.stop()


@pytest.mark.parametrize("age_s", [0.0, CAP - 1, CAP])
async def test_a_snapshot_file_within_the_cap_is_served(tmp_path: Path, age_s: float) -> None:
    h = Harness(tmp_path, snapshot_max_age_s=CAP)
    _file_aged(h, age_s)
    h.loader.fail = True
    await h.manager.start()
    assert h.snap.source == "file"
    assert set(h.snap.apis) == {"old"}
    await h.manager.stop()


async def test_a_snapshot_stamped_in_the_future_is_served(tmp_path: Path) -> None:
    h = Harness(tmp_path, snapshot_max_age_s=CAP)
    _file_aged(h, -120.0)  # clock skew between the writer and this process
    h.loader.fail = True
    await h.manager.start()
    assert h.snap.source == "file"
    await h.manager.stop()


async def test_without_a_cap_any_age_is_served(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _file_aged(h, 10_000 * CAP)
    h.loader.fail = True
    await h.manager.start()
    assert h.snap.source == "file"
    await h.manager.stop()


@pytest.mark.req("REQ-8-2")
async def test_no_postgres_and_no_file_logs_an_error_and_retries_every_5s(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    h.loader.fail = True
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await h.manager.start()
        assert h.store.current is None
        assert h.manager.status() == ConfigStatus("none", 0, 0, None, None)
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "no configuration available" in errors[0].getMessage()
        assert str(h.file.path) in errors[0].getMessage()
        assert h.loader.calls == ["all"]

        await h.clock.step(4.9)
        assert h.loader.calls == ["all"]
        await h.clock.step(0.1)
        assert h.loader.calls == ["all", "all"]
        await h.clock.step(5.0)
        assert h.loader.calls == ["all", "all", "all"]
        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1

        h.loader.fail = False
        await h.clock.step(5.0)
    assert h.snap.source == "postgres"
    assert h.manager.status().apis == 2
    assert "recovered" in caplog.text
    # Loaded: the cadence is now the resync interval, not 5 s.
    calls = len(h.loader.calls)
    await h.clock.step(INTERVAL - 1)
    assert len(h.loader.calls) == calls
    await h.manager.stop()


async def test_start_twice_is_rejected(h: Harness) -> None:
    await h.manager.start()
    with pytest.raises(RuntimeError):
        await h.manager.start()
    await h.manager.stop()


# --- resync ---------------------------------------------------------------------------------


@pytest.mark.req("REQ-5.2-4")
async def test_resync_fires_every_interval_and_replaces_the_snapshot(h: Harness) -> None:
    await _started(h)
    first = h.snap
    h.loader.apis = {"a3": _api("a3")}
    await h.clock.step(INTERVAL - 0.5)
    assert h.snap is first
    await h.clock.step(0.5)
    assert set(h.snap.apis) == {"a3"}
    assert h.snap.synced_at == h.clock.now()
    assert h.manager.status().last_sync_ok == h.clock.now()
    h.loader.apis = {}
    await h.clock.step(INTERVAL)
    assert h.snap.apis == {}
    assert h.loader.calls == ["all", "all", "all"]
    assert len(h.fake_writer.submitted) == 3
    await h.manager.stop()


@pytest.mark.req("REQ-8-2")
async def test_failed_resync_keeps_the_snapshot_and_logs_once_per_streak(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    await _started(h)
    before = h.snap
    synced = h.manager.status().last_sync_ok
    h.loader.fail = True
    with caplog.at_level(logging.INFO, logger=LOGGER):
        for _ in range(3):
            await h.clock.step(INTERVAL)
        assert h.snap is before
        assert h.manager.status().last_sync_ok == synced
        assert h.manager.status().source == "postgres"
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert errors == [f"configuration resync failed: {LOAD_ERROR}; serving the last snapshot"]

        h.loader.fail = False
        await h.clock.step(INTERVAL)
        assert any("recovered" in r.getMessage() for r in caplog.records)
        h.loader.fail = True
        await h.clock.step(INTERVAL)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2
    await h.manager.stop()


async def test_resync_returns_whether_it_succeeded(h: Harness) -> None:
    await _started(h)
    assert await h.manager.resync() is True
    h.loader.fail = True
    assert await h.manager.resync() is False
    await h.manager.stop()


async def test_resync_carries_over_the_previous_entry_of_an_invalid_row(h: Harness) -> None:
    await _started(h)
    h.loader.apis["a1"] = InvalidApi("a1")
    h.loader.apis["fresh-bad"] = InvalidApi("fresh-bad")
    assert await h.manager.resync() is True
    assert h.snap.api_by_id("a1") == _api("a1", 4)
    assert h.snap.api_by_id("fresh-bad") is None
    await h.manager.stop()


async def test_a_carried_entry_never_shadows_a_valid_api_holding_its_slug(h: Harness) -> None:
    await _started(h)
    # a1 (slug "a1") became invalid, and a3 now owns slug "a1" in the same project.
    h.loader.apis = {"a1": InvalidApi("a1"), "a3": _api("a3", 1, "p1", slug="a1")}
    assert await h.manager.resync() is True
    assert h.snap.api_by_id("a1") is None
    assert h.snap.api("p1", "a1") == _api("a3", 1, "p1", slug="a1")
    await h.manager.stop()


# --- events ---------------------------------------------------------------------------------


@pytest.mark.req("REQ-5.2-3")
async def test_api_changed_reloads_the_api(h: Harness) -> None:
    await _started(h)
    synced = h.snap.synced_at
    h.loader.apis["a1"] = _api("a1", 5)
    await h.manager.handle_event(ApiChanged("a1", 5))
    assert h.snap.api_by_id("a1") == _api("a1", 5)
    assert h.snap.synced_at == synced  # a targeted refresh is not a full sync
    assert h.fake_writer.submitted[-1] is h.snap
    await h.manager.stop()


@pytest.mark.req("REQ-5.2-3")
async def test_an_older_event_after_a_newer_load_never_downgrades(h: Harness) -> None:
    await _started(h)
    calls = len(h.loader.calls)
    await h.manager.handle_event(ApiChanged("a1", 3))  # v4 is held
    assert h.snap.api_by_id("a1") == _api("a1", 4)
    assert len(h.loader.calls) == calls  # already current: no Postgres read

    h.loader.apis["a1"] = _api("a1", 3)  # a lagging read must not win either
    await h.manager.handle_event(ApiChanged("a1", 9))
    await h.manager.handle_event(Invalidated("api", "a1"))
    assert h.snap.api_by_id("a1") == _api("a1", 4)
    await h.manager.stop()


async def test_events_for_unknown_ids_are_no_ops(h: Harness) -> None:
    await _started(h)
    before = h.snap
    submitted = len(h.fake_writer.submitted)
    await h.manager.handle_event(ApiChanged("nope", 1))
    await h.manager.handle_event(Invalidated("api", "nope"))
    await h.manager.handle_event(Invalidated("key", "nope"))
    await h.manager.handle_event(Invalidated("project", "nope"))
    assert h.snap is before
    assert len(h.fake_writer.submitted) == submitted
    await h.manager.stop()


@pytest.mark.req("REQ-5.2-3")
async def test_invalidated_api_for_a_deleted_api_removes_it(h: Harness) -> None:
    await _started(h)
    del h.loader.apis["a1"]
    await h.manager.handle_event(Invalidated("api", "a1"))
    assert h.snap.api_by_id("a1") is None
    assert h.snap.api_by_id("a2") is not None
    await h.manager.stop()


async def test_a_bad_row_on_a_targeted_refresh_keeps_the_previous_entry(h: Harness) -> None:
    await _started(h)
    before = h.snap
    h.loader.apis["a1"] = InvalidApi("a1")
    await h.manager.handle_event(ApiChanged("a1", 5))
    await h.manager.handle_event(Invalidated("api", "a1"))
    assert h.snap is before
    await h.manager.stop()


@pytest.mark.req("REQ-4.2-6", "REQ-11.1-6")
async def test_invalidated_key_flips_revoked(h: Harness) -> None:
    await _started(h)
    assert h.snap.key("h1") == _key("k1")
    h.loader.keys["h1"] = _key("k1", revoked=True)
    await h.manager.handle_event(Invalidated("key", "k1"))
    entry = h.snap.key("h1")
    assert entry is not None
    assert entry.revoked is True
    await h.manager.stop()


async def test_invalidated_key_adds_a_new_key_and_removes_a_deleted_one(h: Harness) -> None:
    await _started(h)
    h.loader.keys["h9"] = _key("k9")
    await h.manager.handle_event(Invalidated("key", "k9"))
    assert h.snap.key("h9") == _key("k9")
    del h.loader.keys["h9"]
    await h.manager.handle_event(Invalidated("key", "k9"))
    assert h.snap.key("h9") is None
    assert h.snap.key("h1") is not None
    await h.manager.stop()


@pytest.mark.req("REQ-5.2-3")
async def test_invalidated_project_for_a_gone_project_removes_its_entries(h: Harness) -> None:
    await _started(h)
    h.loader.apis = {"a2": _api("a2", 1, "p2")}
    h.loader.keys = {"h2": _key("k2", "p2")}
    await h.manager.handle_event(Invalidated("project", "p1"))
    assert set(h.snap.apis) == {"a2"}
    assert set(h.snap.keys) == {"h2"}
    await h.manager.stop()


async def test_invalidated_project_reloads_a_live_project(h: Harness) -> None:
    await _started(h)
    h.loader.apis = {"a1": _api("a1", 6), "a5": _api("a5"), "a2": _api("a2", 1, "p2")}
    h.loader.keys = {"h2": _key("k2", "p2"), "h5": _key("k5")}
    await h.manager.handle_event(Invalidated("project", "p1"))
    assert set(h.snap.apis) == {"a1", "a5", "a2"}
    assert h.snap.api_by_id("a1") == _api("a1", 6)
    assert set(h.snap.keys) == {"h2", "h5"}
    await h.manager.stop()


async def test_invalidated_project_keeps_the_previous_entry_of_a_bad_row(h: Harness) -> None:
    await _started(h)
    h.loader.apis["a1"] = InvalidApi("a1")
    h.loader.apis["a1-new-bad"] = InvalidApi("a1-new-bad")
    await h.manager.handle_event(Invalidated("project", "p1"))
    assert h.snap.api_by_id("a1") == _api("a1", 4)
    assert h.snap.api_by_id("a1-new-bad") is None
    await h.manager.stop()


async def test_events_before_any_snapshot_are_ignored(h: Harness) -> None:
    h.loader.fail = True
    await h.manager.start()
    h.loader.fail = False
    await h.manager.handle_event(ApiChanged("a1", 4))
    assert h.store.current is None
    assert h.loader.calls == ["all"]
    await h.manager.stop()


async def test_a_failed_targeted_refresh_keeps_the_snapshot_and_logs_once(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    await _started(h)
    before = h.snap
    h.loader.fail = True
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await h.manager.handle_event(ApiChanged("a1", 9))
        await h.manager.handle_event(Invalidated("key", "k1"))
        await h.manager.handle_event(Invalidated("project", "p1"))
    assert h.snap is before
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
    await h.manager.stop()


async def test_events_are_applied_one_at_a_time_and_none_is_dropped(h: Harness) -> None:
    await _started(h)
    h.loader.gate = asyncio.Event()
    h.loader.apis.update({f"b{i}": _api(f"b{i}", 1) for i in range(20)})
    tasks = [
        asyncio.create_task(h.manager.handle_event(Invalidated("api", f"b{i}"))) for i in range(20)
    ]
    resync = asyncio.create_task(h.manager.resync())
    await settle()
    assert h.loader.in_flight == 1
    h.loader.gate.set()
    await asyncio.gather(*tasks, resync)
    assert h.loader.max_in_flight == 1
    assert all(h.snap.api_by_id(f"b{i}") is not None for i in range(20))
    assert h.loader.calls[1:] == [f"api:b{i}" for i in range(20)] + ["all"]
    await h.manager.stop()


# --- status, stop, subscriber ---------------------------------------------------------------


async def test_status_reports_counts_and_times(h: Harness) -> None:
    await _started(h)
    now = h.clock.now()
    assert h.manager.status() == ConfigStatus("postgres", 2, 2, now, now)
    h.loader.fail = True
    await h.clock.step(INTERVAL)
    status = h.manager.status()
    assert (status.synced_at, status.last_sync_ok) == (now, now)
    assert h.clock.now() - now == INTERVAL
    await h.manager.stop()


async def test_status_from_the_file_has_no_successful_sync(h: Harness) -> None:
    h.file.write(Snapshot({"x": _api("x")}, {}, "postgres", 5.0), written_at=5.0)
    h.loader.fail = True
    await h.manager.start()
    assert h.manager.status() == ConfigStatus("file", 1, 0, 5.0, None)
    await h.manager.stop()


async def test_stop_cancels_background_tasks_and_closes_everything(h: Harness) -> None:
    await _started(h)
    h.loader.gate = asyncio.Event()  # the next resync hangs in the loader
    await h.clock.step(INTERVAL)
    assert h.loader.in_flight == 1
    await asyncio.wait_for(h.manager.stop(), timeout=2)
    assert h.fake_writer.closed
    assert h.loader.closed
    assert [t for t in asyncio.all_tasks() if "ConfigManager" in t.get_name()] == []


async def test_stop_without_start_closes_the_writer_and_loader(h: Harness) -> None:
    await h.manager.stop()
    assert h.fake_writer.closed
    assert h.loader.closed


async def test_a_crashing_supervisor_step_is_logged_and_the_loop_continues(
    h: Harness, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _started(h)
    real = h.loader.load_all
    boom = {"left": 1}

    async def flaky(now: float) -> tuple[Snapshot, frozenset[str]]:
        if boom["left"]:
            boom["left"] -= 1
            raise KeyError("SECRET-MARKER")
        return await real(now)

    monkeypatch.setattr(h.loader, "load_all", flaky)
    h.loader.apis = {"z": _api("z")}
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await h.clock.step(INTERVAL)
        assert "KeyError" in caplog.text
        assert "SECRET-MARKER" not in caplog.text
        await h.clock.step(INTERVAL)
    assert set(h.snap.apis) == {"z"}
    await h.manager.stop()


async def test_the_subscriber_feeds_events_and_resyncs_on_connect(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.loader.apis = {"a1": _api("a1", 1)}
    seen: dict[str, object] = {}
    done = asyncio.Event()

    async def fake_subscribe(
        url: str,
        on_event: Callable[[ConfigEvent], Awaitable[None]],
        on_connected: Callable[[], Awaitable[None]],
        clock: Clock,
        stop: asyncio.Event,
        *,
        on_disconnected: Callable[[], None],
    ) -> None:
        seen["url"], seen["clock"] = url, clock
        h.loader.apis["a1"] = _api("a1", 2)
        await on_connected()
        h.loader.apis["a1"] = _api("a1", 3)
        await on_event(ApiChanged("a1", 3))
        done.set()
        await stop.wait()

    manager = ConfigManager(
        h.loader,
        h.file,
        h.fake_writer,
        h.store,
        h.clock,
        resync_interval_s=INTERVAL,
        redis_url="redis://r:6379/0",
        subscriber=fake_subscribe,
    )
    await manager.start()
    await asyncio.wait_for(done.wait(), timeout=2)
    assert h.snap.api_by_id("a1") == _api("a1", 3)
    assert h.loader.calls == ["all", "all", "api:a1"]
    assert seen["url"] == "redis://r:6379/0"
    assert seen["clock"] is h.clock
    await asyncio.wait_for(manager.stop(), timeout=2)


async def test_a_subscriber_that_dies_is_logged_not_fatal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    h = Harness(tmp_path)

    async def dying_subscribe(*args: object, **kwargs: object) -> None:
        raise RuntimeError("SECRET-MARKER")

    manager = ConfigManager(
        h.loader,
        h.file,
        h.fake_writer,
        h.store,
        h.clock,
        resync_interval_s=INTERVAL,
        redis_url="redis://r:6379/0",
        subscriber=dying_subscribe,
    )
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await manager.start()
        await settle()
    assert "RuntimeError" in caplog.text
    assert "SECRET-MARKER" not in caplog.text
    assert "redis://" not in caplog.text
    await h.clock.step(INTERVAL)  # the supervisor still runs
    assert h.loader.calls == ["all", "all"]
    await manager.stop()


async def test_resync_runs_at_the_retry_interval_while_the_subscriber_is_down(
    tmp_path: Path,
) -> None:
    """Revocations are pushed over Redis; with it down, Postgres is polled every 5 s, not 60 s."""
    h = Harness(tmp_path)
    lost = asyncio.Event()
    reconnect = asyncio.Event()

    async def flaky_subscribe(
        url: str,
        on_event: Callable[[ConfigEvent], Awaitable[None]],
        on_connected: Callable[[], Awaitable[None]],
        clock: Clock,
        stop: asyncio.Event,
        *,
        on_disconnected: Callable[[], None],
    ) -> None:
        await on_connected()
        await lost.wait()
        on_disconnected()
        await reconnect.wait()
        await on_connected()
        await stop.wait()

    manager = ConfigManager(
        h.loader,
        h.file,
        h.fake_writer,
        h.store,
        h.clock,
        resync_interval_s=INTERVAL,
        redis_url="redis://r:6379/0",
        subscriber=flaky_subscribe,
    )
    await manager.start()
    await settle()
    assert h.loader.calls == ["all", "all"]  # startup load + resync on connect
    lost.set()
    await settle()
    await h.clock.step(RETRY_INTERVAL_S)
    assert h.loader.calls.count("all") == 3  # polled well before INTERVAL
    await h.clock.step(RETRY_INTERVAL_S)
    assert h.loader.calls.count("all") == 4
    reconnect.set()
    await settle()
    await h.clock.step(RETRY_INTERVAL_S)  # the sleep already in flight finishes
    calls = h.loader.calls.count("all")
    await h.clock.step(RETRY_INTERVAL_S)
    assert h.loader.calls.count("all") == calls  # back to the slow interval
    await manager.stop()


async def test_a_short_resync_interval_is_kept_while_the_subscriber_is_down(
    tmp_path: Path,
) -> None:
    """The subscriber being down only ever shortens the wait: a 1 s interval stays 1 s."""
    h = Harness(tmp_path)

    async def always_down(
        url: str,
        on_event: Callable[[ConfigEvent], Awaitable[None]],
        on_connected: Callable[[], Awaitable[None]],
        clock: Clock,
        stop: asyncio.Event,
        *,
        on_disconnected: Callable[[], None],
    ) -> None:
        on_disconnected()
        await stop.wait()

    manager = ConfigManager(
        h.loader,
        h.file,
        h.fake_writer,
        h.store,
        h.clock,
        resync_interval_s=1.0,
        redis_url="redis://r:6379/0",
        subscriber=always_down,
    )
    await manager.start()
    await settle()
    before = h.loader.calls.count("all")
    await h.clock.step(1.0)
    assert h.loader.calls.count("all") == before + 1
    await manager.stop()


def test_resync_interval_must_be_positive(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    with pytest.raises(ValueError, match="resync_interval_s"):
        ConfigManager(
            h.loader, h.file, h.fake_writer, h.store, h.clock, resync_interval_s=0, redis_url=None
        )


async def test_project_reload_drops_a_bad_rows_entry_whose_slug_was_taken(h: Harness) -> None:
    await _started(h)
    h.loader.apis = {"a1": InvalidApi("a1"), "a3": _api("a3", 1, "p1", slug="a1")}
    await h.manager.handle_event(Invalidated("project", "p1"))
    assert h.snap.api_by_id("a1") is None
    assert h.snap.api("p1", "a1") == _api("a3", 1, "p1", slug="a1")
    await h.manager.stop()


# --- fix round 1 ----------------------------------------------------------------------------


async def test_startup_logs_the_loader_reason_never_the_dsn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A misconfigured DSN is reported by its (DSN-free) reason, not as "unreachable"."""
    h = Harness(tmp_path)
    loader = PostgresLoader("postgresql://u:pw-SECRET-MARKER@db.internal:5432/jarvis?options=x")
    manager = ConfigManager(
        loader, h.file, h.fake_writer, h.store, h.clock, resync_interval_s=INTERVAL, redis_url=None
    )
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await manager.start()
        await h.clock.step(15.0)  # three more failed attempts, same reason
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "no configuration available" in errors[0]
    assert "unsupported DSN option: options" in errors[0]
    assert "pw-SECRET-MARKER" not in caplog.text
    assert "db.internal" not in caplog.text
    assert "postgresql://" not in caplog.text
    await manager.stop()


async def test_startup_from_the_file_names_the_reason(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    h.file.write(Snapshot({}, {}, "postgres", 5.0), written_at=5.0)
    h.loader.fail = True
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await h.manager.start()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert LOAD_ERROR in warnings[0]
    assert str(h.file.path) in warnings[0]
    await h.manager.stop()


async def test_a_changed_failure_reason_is_logged_again(
    h: Harness, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    reasons = iter(["reason A", "reason A", "reason B", "reason B"])

    async def failing(now: float) -> tuple[Snapshot, frozenset[str]]:
        raise ConfigLoadError(next(reasons))

    monkeypatch.setattr(h.loader, "load_all", failing)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await h.manager.start()
        for _ in range(3):
            await h.clock.step(5.0)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2
    assert "reason A" in errors[0]
    assert "reason B" in errors[1]
    await h.manager.stop()


async def test_a_targeted_success_never_reports_full_resyncs_recovered(
    h: Harness, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _started(h)
    real_all = h.loader.load_all

    async def failing(now: float) -> tuple[Snapshot, frozenset[str]]:
        raise ConfigLoadError(LOAD_ERROR)

    monkeypatch.setattr(h.loader, "load_all", failing)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await h.clock.step(INTERVAL)  # full resync fails
        h.loader.apis["a1"] = _api("a1", 5)
        await h.manager.handle_event(ApiChanged("a1", 5))  # targeted read works
        assert h.snap.api_by_id("a1") == _api("a1", 5)
        assert "recovered" not in caplog.text
        monkeypatch.setattr(h.loader, "load_all", real_all)
        await h.clock.step(INTERVAL)
    recovered = [r.getMessage() for r in caplog.records if "recovered" in r.getMessage()]
    assert recovered == ["configuration resync from Postgres recovered"]
    await h.manager.stop()


async def test_file_mode_does_not_report_recovered_before_a_full_load(
    h: Harness, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.file.write(Snapshot({"a1": _api("a1", 1)}, {}, "postgres", 5.0), written_at=5.0)
    real_all = h.loader.load_all

    async def failing(now: float) -> tuple[Snapshot, frozenset[str]]:
        raise ConfigLoadError(LOAD_ERROR)

    monkeypatch.setattr(h.loader, "load_all", failing)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await h.manager.start()
        await h.manager.handle_event(ApiChanged("a1", 4))  # targeted refresh succeeds
        assert h.snap.api_by_id("a1") == _api("a1", 4)
        assert h.snap.source == "file"
        assert "recovered" not in caplog.text
        monkeypatch.setattr(h.loader, "load_all", real_all)
        await h.clock.step(5.0)
    assert h.snap.source == "postgres"
    assert "configuration resync from Postgres recovered" in caplog.text
    await h.manager.stop()


@pytest.mark.req("REQ-4.2-6", "REQ-11.1-6")
async def test_a_failed_targeted_refresh_triggers_an_early_resync(
    h: Harness, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revoke that hits a transient Postgres error lands within ~5 s, not the 60 s resync."""
    await _started(h)
    await h.clock.step(10.0)
    real_key = h.loader.load_key

    async def failing_key(key_id: str) -> tuple[str, KeyEntry] | None:
        raise ConfigLoadError(LOAD_ERROR)

    monkeypatch.setattr(h.loader, "load_key", failing_key)
    h.loader.keys["h1"] = _key("k1", revoked=True)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await h.manager.handle_event(Invalidated("key", "k1"))
    assert h.snap.key("h1") == _key("k1")  # not applied yet
    assert "a full resync will follow" in caplog.text
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await settle()  # 10 s since the last supervised run: the early resync may run at once
    assert "config event refresh from Postgres recovered" in caplog.text
    revoked = h.snap.key("h1")
    assert revoked is not None
    assert revoked.revoked is True
    monkeypatch.setattr(h.loader, "load_key", real_key)
    await h.manager.stop()


@pytest.mark.req("REQ-4.2-6", "REQ-11.1-6")
async def test_an_unexpected_error_in_a_targeted_refresh_requests_an_early_resync(
    h: Harness, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only ConfigLoadError: any failure leaves nothing escaping and asks for a resync."""
    await _started(h)
    await h.clock.step(10.0)

    async def broken_key(key_id: str) -> tuple[str, KeyEntry] | None:
        raise RuntimeError("SECRET-MARKER")

    monkeypatch.setattr(h.loader, "load_key", broken_key)
    h.loader.keys["h1"] = _key("k1", revoked=True)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await h.manager.handle_event(Invalidated("key", "k1"))  # must not raise
    assert "RuntimeError" in caplog.text
    assert "SECRET-MARKER" not in caplog.text
    monkeypatch.undo()
    await settle()  # the early resync (not the 60 s one) brings the revoke in
    revoked = h.snap.key("h1")
    assert revoked is not None
    assert revoked.revoked is True
    await h.manager.stop()


async def test_early_resyncs_are_rate_limited_and_retried_until_one_succeeds(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _started(h)

    async def failing_key(key_id: str) -> tuple[str, KeyEntry] | None:
        raise ConfigLoadError(LOAD_ERROR)

    monkeypatch.setattr(h.loader, "load_key", failing_key)
    h.loader.keys["h1"] = _key("k1", revoked=True)
    h.loader.fail = True  # the full resyncs fail too, for a while
    await h.manager.handle_event(Invalidated("key", "k1"))
    await h.manager.handle_event(Invalidated("key", "k1"))
    await h.clock.step(4.9)
    assert h.loader.calls == ["all"]  # started 0 s ago: the first early resync waits 5 s
    await h.clock.step(0.1)
    assert h.loader.calls == ["all", "all"]
    await h.clock.step(5.0)  # still pending: retried every 5 s, not every 60 s
    assert h.loader.calls == ["all", "all", "all"]
    h.loader.fail = False
    await h.clock.step(5.0)
    entry = h.snap.key("h1")
    assert entry is not None
    assert entry.revoked is True
    calls = len(h.loader.calls)
    await h.clock.step(INTERVAL - 1)  # back to the normal cadence
    assert len(h.loader.calls) == calls
    await h.manager.stop()


@pytest.mark.req("REQ-4.2-6", "REQ-11.1-6")
async def test_a_revoke_during_an_in_flight_resync_is_not_lost(h: Harness) -> None:
    """The resync read old rows; the revoke event must apply after it, re-reading the key."""
    await _started(h)
    h.loader.all_gate = asyncio.Event()
    resync = asyncio.create_task(h.manager.resync())
    await settle()
    assert h.loader.calls[-1] == "all:read"  # old, non-revoked rows are in flight
    h.loader.keys["h1"] = _key("k1", revoked=True)
    event = asyncio.create_task(h.manager.handle_event(Invalidated("key", "k1")))
    await settle()
    h.loader.all_gate.set()
    await asyncio.gather(resync, event)
    entry = h.snap.key("h1")
    assert entry is not None
    assert entry.revoked is True
    await h.manager.stop()


async def test_stop_during_start_spawns_nothing(h: Harness) -> None:
    h.loader.all_gate = asyncio.Event()
    start = asyncio.create_task(h.manager.start())
    await settle()
    await h.manager.stop()
    h.loader.all_gate.set()
    await asyncio.wait_for(start, timeout=2)
    await settle()
    assert [t for t in asyncio.all_tasks() if "ConfigManager" in t.get_name()] == []


async def test_an_event_of_an_unknown_kind_is_ignored(h: Harness) -> None:
    await _started(h)
    before = h.snap
    submitted = len(h.fake_writer.submitted)
    await h.manager.handle_event(Invalidated("tenant", "t1"))  # type: ignore[arg-type]
    assert h.snap is before
    assert len(h.fake_writer.submitted) == submitted
    await h.manager.stop()
