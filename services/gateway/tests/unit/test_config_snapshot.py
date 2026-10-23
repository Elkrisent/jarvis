import json
from typing import Any

import pytest
from jarvis_core.config import ApiDependency
from jarvis_gateway.config.snapshot import ApiEntry, KeyEntry, Snapshot

pytestmark = pytest.mark.unit


def _api(api_id: str, project: str, slug: str, version: int = 1, **cfg: Any) -> ApiEntry:
    raw = {"slug": slug, "name": slug.title(), "base_url": "https://api.example.com"} | cfg
    return ApiEntry(api_id, project, slug, version, ApiDependency.model_validate(raw))


def _key(
    key_id: str, project: str, allowed: tuple[str, ...] = (), revoked: bool = False
) -> KeyEntry:
    return KeyEntry(key_id, project, frozenset(allowed), revoked)


def _snapshot() -> Snapshot:
    cred = {"credential": {"mode": "header", "header_name": "x-token", "secret_ref": "sec_1"}}
    apis = [
        _api("a1", "p1", "weather", 3, **cred),
        _api("a2", "p1", "maps"),
        _api("a3", "p2", "weather", 2),
    ]
    keys = {
        "h-live": _key("k1", "p1", ("a1", "a2")),
        "h-dead": _key("k2", "p2", ("a3",), revoked=True),
    }
    return Snapshot.empty("postgres", 100.0).with_changes(
        apis={a.api_id: a for a in apis}, keys=keys, synced_at=123.5
    )


@pytest.mark.req("REQ-5.2-3")
def test_round_trip_and_json_dumpable() -> None:
    snap = _snapshot()
    data = snap.to_json_dict()
    restored = Snapshot.from_json_dict(json.loads(json.dumps(data)))
    assert restored == snap
    assert restored.key("h-dead") == _key("k2", "p2", ("a3",), revoked=True)
    assert restored.api("p2", "weather") == snap.api_by_id("a3")
    assert restored.source == "postgres"
    assert restored.synced_at == 123.5


@pytest.mark.req("REQ-5.2-3")
def test_lookups() -> None:
    snap = _snapshot()
    assert snap.api("p1", "weather") is snap.api_by_id("a1")
    assert snap.api("p2", "weather") is snap.api_by_id("a3")
    assert snap.api("p1", "nope") is None
    assert snap.api_by_id("zzz") is None
    assert snap.key("missing") is None


def _bad(**changes: Any) -> dict[str, Any]:
    data = _snapshot().to_json_dict()
    data.update(changes)
    return data


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(_bad(format=2), id="wrong-format"),
        pytest.param({k: v for k, v in _bad().items() if k != "apis"}, id="missing-apis"),
        pytest.param({k: v for k, v in _bad().items() if k != "keys"}, id="missing-keys"),
        pytest.param(_bad(source="mars"), id="bad-source"),
        pytest.param(_bad(synced_at="soon"), id="bad-synced-at"),
        pytest.param(_bad(apis="nope"), id="apis-not-list"),
        pytest.param(_bad(apis=[1]), id="api-not-object"),
        pytest.param(_bad(keys=[1]), id="key-not-object"),
        pytest.param(_bad(apis=[{"api_id": 5}]), id="non-string-api-id"),
        pytest.param(
            _bad(
                keys=[
                    {
                        "key_hash": "h",
                        "key_id": 1,
                        "project_id": "p",
                        "allowed_api_ids": [],
                        "revoked": False,
                    }
                ]
            ),
            id="non-string-key-id",
        ),
        pytest.param(
            _bad(
                keys=[
                    {
                        "key_hash": "h",
                        "key_id": "k",
                        "project_id": "p",
                        "allowed_api_ids": [3],
                        "revoked": False,
                    }
                ]
            ),
            id="non-string-allowed",
        ),
        pytest.param(
            _bad(
                keys=[
                    {
                        "key_hash": "h",
                        "key_id": "k",
                        "project_id": "p",
                        "allowed_api_ids": [],
                        "revoked": "no",
                    }
                ]
            ),
            id="non-bool-revoked",
        ),
    ],
)
def test_from_json_dict_rejects_invalid(data: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="."):
        Snapshot.from_json_dict(data)


def test_from_json_dict_rejects_non_mapping() -> None:
    with pytest.raises(ValueError, match="."):
        Snapshot.from_json_dict([])


def test_invalid_api_config_error_names_api_id_not_config() -> None:
    data = _snapshot().to_json_dict()
    apis: list[dict[str, Any]] = data["apis"]  # type: ignore[assignment]
    apis[0]["config"]["base_url"] = "ftp://secret-host.example/sec_1"
    apis[0]["config"]["bogus"] = "TOPSECRET"
    with pytest.raises(ValueError, match="invalid config") as exc:
        Snapshot.from_json_dict(data)
    message = str(exc.value)
    assert apis[0]["api_id"] in message
    assert "TOPSECRET" not in message
    assert "secret-host" not in message
    assert "sec_1" not in message


def test_version_must_be_int() -> None:
    data = _snapshot().to_json_dict()
    data["apis"][0]["version"] = "3"  # type: ignore[index]
    with pytest.raises(ValueError, match="."):
        Snapshot.from_json_dict(data)


@pytest.mark.req("REQ-4.2-6")
def test_with_changes_add_replace_remove_key() -> None:
    snap = _snapshot()
    revoked = _key("k1", "p1", ("a1", "a2"), revoked=True)
    snap2 = snap.with_changes(keys={"h-live": revoked, "h-new": _key("k3", "p1")})
    assert snap2.key("h-live") == revoked
    assert snap2.key("h-new") is not None
    assert snap.key("h-live") == _key("k1", "p1", ("a1", "a2"))  # original untouched
    assert snap.key("h-new") is None
    snap3 = snap2.with_changes(keys={"h-live": None})
    assert snap3.key("h-live") is None
    assert snap3.key("h-new") is not None


def test_with_changes_add_replace_remove_api() -> None:
    snap = _snapshot()
    added = _api("a4", "p1", "news")
    snap2 = snap.with_changes(apis={"a4": added})
    assert snap2.api("p1", "news") is added
    assert snap.api("p1", "news") is None
    snap3 = snap2.with_changes(apis={"a4": None, "ghost": None})
    assert snap3.api("p1", "news") is None
    assert snap3.api_by_id("a4") is None


def test_lower_version_ignored_equal_or_higher_replaces() -> None:
    snap = _snapshot()  # a1 at version 3
    older = _api("a1", "p1", "weather", 2, name="Old")
    assert snap.with_changes(apis={"a1": older}).api_by_id("a1") == snap.api_by_id("a1")
    same = _api("a1", "p1", "weather", 3, name="Same")
    assert snap.with_changes(apis={"a1": same}).api_by_id("a1") is same
    newer = _api("a1", "p1", "weather", 4, name="New")
    assert snap.with_changes(apis={"a1": newer}).api_by_id("a1") is newer


def test_slug_change_moves_the_entry() -> None:
    snap = _snapshot()
    renamed = _api("a1", "p1", "forecast", 4)
    snap2 = snap.with_changes(apis={"a1": renamed})
    assert snap2.api("p1", "weather") is None
    assert snap2.api("p1", "forecast") is renamed
    assert snap2.api_by_id("a1") is renamed


def test_remove_project_removes_its_apis_and_keys() -> None:
    snap = _snapshot()
    snap2 = snap.with_changes(remove_project="p1")
    assert snap2.api_by_id("a1") is None
    assert snap2.api_by_id("a2") is None
    assert snap2.api("p1", "weather") is None
    assert snap2.key("h-live") is None
    assert snap2.api("p2", "weather") is not None
    assert snap2.key("h-dead") is not None
    assert snap.api("p1", "weather") is not None


def test_with_changes_source_and_synced_at() -> None:
    snap = _snapshot()
    assert snap.with_changes().source == "postgres"
    assert snap.with_changes().synced_at == snap.synced_at
    snap2 = snap.with_changes(source="file", synced_at=5.0)
    assert (snap2.source, snap2.synced_at) == ("file", 5.0)


def test_mappings_are_read_only() -> None:
    snap = _snapshot()
    with pytest.raises(TypeError):
        snap.apis["x"] = _api("x", "p", "x")  # type: ignore[index]
    with pytest.raises(TypeError):
        snap.keys["x"] = _key("x", "p")  # type: ignore[index]


def test_empty_snapshot() -> None:
    snap = Snapshot.empty("file", 1.0)
    assert (len(snap.apis), len(snap.keys), snap.source) == (0, 0, "file")


@pytest.mark.req("REQ-5.2-3")
def test_repr_shows_counts_only() -> None:
    text = repr(_snapshot())
    assert "apis=3" in text
    assert "keys=2" in text
    for leaked in ("weather", "maps", "sec_1", "h-live", "h-dead", "x-token"):
        assert leaked not in text


def test_entry_reprs_omit_config_and_allowed_ids() -> None:
    api = _api("a1", "p1", "weather", credential={"mode": "bearer", "secret_ref": "sec_1"})
    assert "sec_1" not in repr(api)
    assert "weather" not in repr(api)
    assert "zzz-api" not in repr(_key("k1", "p1", ("zzz-api",)))


@pytest.mark.parametrize(
    "value",
    [10**400, float("nan"), float("inf"), float("-inf"), True, "1.0", None],
    ids=["huge-int", "nan", "inf", "-inf", "bool", "string", "null"],
)
def test_synced_at_must_be_a_finite_number(value: object) -> None:
    with pytest.raises(ValueError, match="synced_at"):
        Snapshot.from_json_dict(_bad(synced_at=value))


@pytest.mark.parametrize("value", [0, -1, True, 2**63, 1.5, "1"], ids=str)
def test_api_version_must_be_a_sane_positive_int(value: object) -> None:
    data = _snapshot().to_json_dict()
    data["apis"][0]["version"] = value  # type: ignore[index]
    with pytest.raises(ValueError, match="version"):
        Snapshot.from_json_dict(data)


def test_duplicate_api_id_rejected() -> None:
    data = _snapshot().to_json_dict()
    apis: list[dict[str, Any]] = data["apis"]  # type: ignore[assignment]
    apis.append(dict(apis[0], slug="other"))
    with pytest.raises(ValueError, match="duplicate api_id a1"):
        Snapshot.from_json_dict(data)


def test_duplicate_project_slug_rejected() -> None:
    data = _snapshot().to_json_dict()
    apis: list[dict[str, Any]] = data["apis"]  # type: ignore[assignment]
    apis.append(dict(apis[0], api_id="a9"))
    with pytest.raises(ValueError, match="duplicate slug.*a9"):
        Snapshot.from_json_dict(data)


def test_duplicate_key_hash_rejected() -> None:
    data = _snapshot().to_json_dict()
    keys: list[dict[str, Any]] = data["keys"]  # type: ignore[assignment]
    keys.append(dict(keys[0], key_id="k9"))
    with pytest.raises(ValueError, match="duplicate key"):
        Snapshot.from_json_dict(data)


def test_with_changes_rejects_mismatched_mapping_key() -> None:
    with pytest.raises(ValueError, match="a1"):
        _snapshot().with_changes(apis={"a1": _api("a2", "p1", "maps", 9)})


def test_remove_project_applies_before_adds_in_the_same_call() -> None:
    snap = _snapshot()
    readded = _api("a1", "p1", "weather", 1)
    snap2 = snap.with_changes(remove_project="p1", apis={"a1": readded})
    assert snap2.api_by_id("a1") is readded  # removal first, so the stale-guard sees nothing held
    assert snap2.api_by_id("a2") is None
