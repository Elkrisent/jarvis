import asyncio

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


@pytest.mark.req("REQ-5.2-3")
async def test_concurrent_readers_never_see_a_mixed_snapshot() -> None:
    first, second = _versioned(1), _versioned(2)
    store = ConfigStore()
    store.replace(first)
    stop = asyncio.Event()
    observed: set[int] = set()

    async def writer() -> None:
        flip = False
        while not stop.is_set():
            store.replace(second if flip else first)
            flip = not flip
            await asyncio.sleep(0)

    async def reader() -> None:
        for _ in range(60):
            snap = store.current
            assert snap is not None
            versions = {entry.version for entry in snap.apis.values()}
            assert len(versions) == 1, f"mixed snapshot: {versions}"
            observed.update(versions)
            await asyncio.sleep(0)

    writer_task = asyncio.create_task(writer())
    await asyncio.gather(*(reader() for _ in range(20)))
    stop.set()
    await writer_task
    assert observed == {1, 2}  # the swap really happened while readers ran


@pytest.mark.req("REQ-5.2-3")
def test_returned_mappings_cannot_be_mutated() -> None:
    store = ConfigStore()
    store.replace(_versioned(1))
    assert store.current is not None
    with pytest.raises(TypeError):
        store.current.apis["a0"] = store.current.apis["a1"]  # type: ignore[index]
