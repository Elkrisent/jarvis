import asyncio
from collections.abc import Awaitable, Callable

import pytest
from jarvis_core.config import ApiDependency
from jarvis_gateway.config.snapshot import ApiEntry, Snapshot
from jarvis_gateway.config.store import ConfigStore

pytestmark = pytest.mark.unit

N_APIS = 200


def _versioned(version: int) -> Snapshot:
    apis = {
        f"a{i}": ApiEntry(
            f"a{i}",
            "p",
            f"s{i}",
            version,
            ApiDependency.model_validate(
                {"slug": f"s{i}", "name": "n", "base_url": "https://api.example.com"}
            ),
        )
        for i in range(N_APIS)
    }
    return Snapshot.empty("postgres", float(version)).with_changes(apis=apis)


def test_current_is_none_until_replaced() -> None:
    store = ConfigStore()
    assert store.current is None
    snap = Snapshot.empty("file", 1.0)
    store.replace(snap)
    assert store.current is snap


def test_update_applies_fn_to_current() -> None:
    store = ConfigStore()
    store.replace(Snapshot.empty("file", 1.0))
    result = store.update(lambda s: s.with_changes(synced_at=9.0))
    assert result.synced_at == 9.0
    assert store.current is result


def test_update_without_snapshot_raises() -> None:
    with pytest.raises(RuntimeError):
        ConfigStore().update(lambda s: s)


def _bump(snap: Snapshot, only: range | None = None) -> Snapshot:
    """One with_changes that raises the version of many APIs at once."""
    ids = [f"a{i}" for i in (only if only is not None else range(N_APIS))]
    changes = {}
    for api_id in ids:
        held = snap.apis[api_id]
        changes[api_id] = ApiEntry(
            api_id, held.project_id, held.slug, held.version + 1, held.config
        )
    return snap.with_changes(apis=changes)


class _IncrementalStore(ConfigStore):
    """Deliberately non-atomic: publishes half-applied state, yielding in between."""

    async def update_incrementally(self) -> None:
        half = N_APIS // 2
        self.update(lambda s: _bump(s, range(half)))
        await asyncio.sleep(0)
        self.update(lambda s: _bump(s, range(half, N_APIS)))


async def _count_violations(store: ConfigStore, write_once: Callable[[], Awaitable[None]]) -> int:
    stop = asyncio.Event()
    violations = 0
    observed: set[int] = set()

    async def writer() -> None:
        while not stop.is_set():
            await write_once()
            await asyncio.sleep(0)

    async def reader() -> None:
        nonlocal violations
        for _ in range(60):
            snap = store.current
            assert snap is not None
            before = {e.version for e in snap.apis.values()}
            await asyncio.sleep(0)  # hold the snapshot across a writer turn
            after = {e.version for e in snap.apis.values()}
            if len(before) != 1 or before != after:
                violations += 1
            observed.update(before)

    writer_task = asyncio.create_task(writer())
    await asyncio.gather(*(reader() for _ in range(20)))
    stop.set()
    await writer_task
    assert len(observed) > 1  # the store really changed while readers were running
    return violations


@pytest.mark.req("REQ-5.2-3")
async def test_concurrent_readers_never_see_a_mixed_snapshot() -> None:
    store = ConfigStore()
    store.replace(_versioned(1))

    async def write_once() -> None:
        store.update(_bump)

    assert await _count_violations(store, write_once) == 0


async def test_reader_check_detects_a_non_atomic_store() -> None:
    store = _IncrementalStore()
    store.replace(_versioned(1))
    assert await _count_violations(store, store.update_incrementally) > 0


@pytest.mark.req("REQ-5.2-3")
def test_returned_mappings_cannot_be_mutated() -> None:
    store = ConfigStore()
    store.replace(_versioned(1))
    assert store.current is not None
    with pytest.raises(TypeError):
        store.current.apis["a0"] = store.current.apis["a1"]  # type: ignore[index]


def test_listeners_see_every_installed_snapshot_and_a_late_one_gets_the_current_one() -> None:
    store = ConfigStore()
    seen: list[Snapshot] = []
    first = Snapshot.empty("postgres", 1.0)
    store.replace(first)
    store.add_listener(seen.append)
    assert seen == [first]  # added late: told about the snapshot already installed
    second = Snapshot.empty("postgres", 2.0)
    store.replace(second)
    third = store.update(lambda s: s.with_changes(synced_at=3.0))
    assert seen == [first, second, third]
    assert store.current is third


def test_a_failing_listener_does_not_stop_the_swap_or_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = ConfigStore()
    seen: list[Snapshot] = []

    def broken(snapshot: Snapshot) -> None:
        raise RuntimeError("boom with a secret value")

    store.add_listener(broken)
    store.add_listener(seen.append)
    snap = Snapshot.empty("postgres", 1.0)
    with caplog.at_level("WARNING"):
        store.replace(snap)
    assert store.current is snap
    assert seen == [snap]
    assert "RuntimeError" in caplog.text
    assert "secret value" not in caplog.text
