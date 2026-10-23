import logging
from typing import Any

import pytest
from jarvis_core.config import ApiDependency
from jarvis_core.errors import AuthInvalidError
from jarvis_core.keys import hash_jarvis_key
from jarvis_gateway.auth import authenticate
from jarvis_gateway.config.snapshot import ApiEntry, KeyEntry, Snapshot

pytestmark = pytest.mark.unit

KEY_A = "jv_live_keyAAAAAAAA"
KEY_B = "jv_live_keyBBBBBBBB"


def _api(api_id: str, project: str, slug: str, **cfg: Any) -> ApiEntry:
    raw = {
        "slug": slug,
        "name": slug,
        "base_url": "https://api.example.com",
        "jarvis_key_slot": "authorization",
    } | cfg
    return ApiEntry(api_id, project, slug, 1, ApiDependency.model_validate(raw))


def _key(
    key_id: str, project: str, allowed: tuple[str, ...] = (), revoked: bool = False
) -> KeyEntry:
    return KeyEntry(key_id, project, frozenset(allowed), revoked)


def _snap(apis: list[ApiEntry], keys: dict[str, KeyEntry]) -> Snapshot:
    hashed = {hash_jarvis_key(k): v for k, v in keys.items()}
    return Snapshot.empty("postgres", 1.0).with_changes(
        apis={a.api_id: a for a in apis}, keys=hashed
    )


def _hdr(key: str) -> list[tuple[str, str]]:
    return [("x-jarvis-key", key)]


def _bearer(key: str) -> list[tuple[str, str]]:
    return [("authorization", f"Bearer {key}")]


def _deny(snap: Snapshot, slug: str, headers: list[tuple[str, str]]) -> AuthInvalidError:
    with pytest.raises(AuthInvalidError) as info:
        authenticate(snap, slug, headers)
    return info.value


@pytest.mark.req("REQ-4.2-2")
def test_valid_header_key_unrestricted_means_every_api_in_the_project() -> None:
    snap = _snap([_api("a1", "p1", "weather")], {KEY_A: _key("k1", "p1")})
    got = authenticate(snap, "weather", _hdr(KEY_A))
    assert (got.auth.project_id, got.auth.key_id, got.api.api_id) == ("p1", "k1", "a1")
    assert got.auth.allowed_api_ids is None
    assert got.auth.allows("anything")


@pytest.mark.req("REQ-4.2-3")
def test_valid_slot_key_authenticates() -> None:
    snap = _snap([_api("a1", "p1", "weather")], {KEY_A: _key("k1", "p1")})
    assert authenticate(snap, "weather", _bearer(KEY_A)).auth.key_id == "k1"


@pytest.mark.req("REQ-4.2-5")
def test_restricted_key_carries_its_allowed_set_and_reaches_a_listed_api() -> None:
    snap = _snap(
        [_api("a1", "p1", "weather"), _api("a2", "p1", "maps")],
        {KEY_A: _key("k1", "p1", ("a1",))},
    )
    got = authenticate(snap, "weather", _hdr(KEY_A))
    assert got.auth.allowed_api_ids == frozenset({"a1"})


@pytest.mark.req("REQ-4.2-5")
def test_restricted_key_cannot_call_another_api() -> None:
    snap = _snap(
        [_api("a1", "p1", "weather"), _api("a2", "p1", "maps")],
        {KEY_A: _key("k1", "p1", ("a1",))},
    )
    _deny(snap, "maps", _hdr(KEY_A))


@pytest.mark.req("REQ-4.2-5")
def test_allowed_set_naming_only_a_deleted_api_is_not_unrestricted() -> None:
    snap = _snap([_api("a1", "p1", "weather")], {KEY_A: _key("k1", "p1", ("gone",))})
    _deny(snap, "weather", _hdr(KEY_A))


@pytest.mark.req("REQ-4.2-4")
def test_revoked_key_is_denied_like_an_unknown_one() -> None:
    snap = _snap([_api("a1", "p1", "weather")], {KEY_A: _key("k1", "p1", revoked=True)})
    revoked = _deny(snap, "weather", _hdr(KEY_A))
    unknown = _deny(snap, "weather", _hdr(KEY_B))
    assert revoked.to_body() == unknown.to_body()
    assert _deny(snap, "weather", _bearer(KEY_A)).to_body() == unknown.to_body()


@pytest.mark.req("REQ-4.2-4")
def test_missing_key_is_denied() -> None:
    snap = _snap([_api("a1", "p1", "weather")], {KEY_A: _key("k1", "p1")})
    assert _deny(snap, "weather", []).code == "JV_AUTH_INVALID"


def test_wrong_project_unknown_slug_and_ambiguous_slug_look_identical() -> None:
    snap = _snap(
        [_api("a1", "p1", "weather"), _api("a9", "p2", "other")],
        {KEY_A: _key("k1", "p1"), KEY_B: _key("k2", "p2")},
    )
    bodies = [
        _deny(snap, "other", _hdr(KEY_A)).to_body(),  # slug exists only in another project
        _deny(snap, "nope", _hdr(KEY_A)).to_body(),  # no such slug
        _deny(snap, "weather", _hdr("jv_live_unknown")).to_body(),
    ]
    assert bodies[0] == bodies[1] == bodies[2]


@pytest.mark.req("REQ-4.2-1")
def test_the_same_slug_in_two_projects_never_crosses_over() -> None:
    snap = _snap(
        [_api("a1", "p1", "weather"), _api("a2", "p2", "weather")],
        {KEY_A: _key("k1", "p1"), KEY_B: _key("k2", "p2")},
    )
    for key, project in ((KEY_A, "p1"), (KEY_B, "p2")):
        for headers in (_hdr(key), _bearer(key)):
            got = authenticate(snap, "weather", headers)
            assert got.api.project_id == project == got.auth.project_id


def test_slot_name_is_per_api() -> None:
    snap = _snap(
        [_api("a1", "p1", "weather", jarvis_key_slot="x-api-key")],
        {KEY_A: _key("k1", "p1")},
    )
    assert authenticate(snap, "weather", [("x-api-key", KEY_A)]).auth.key_id == "k1"
    _deny(snap, "weather", _bearer(KEY_A))


def test_a_duplicate_slug_inside_one_project_fails_closed_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    snap = _snap(
        [_api("a1", "p1", "weather"), _api("a2", "p1", "weather")], {KEY_A: _key("k1", "p1")}
    )
    with caplog.at_level(logging.DEBUG):
        _deny(snap, "weather", _hdr(KEY_A))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "p1" in warnings[0].getMessage()


def test_the_error_names_neither_key_nor_hash_nor_slug() -> None:
    snap = _snap([_api("a1", "p1", "weather")], {KEY_A: _key("k1", "p1", revoked=True)})
    err = _deny(snap, "weather", _hdr(KEY_A))
    text = str(err) + str(err.to_body())
    assert KEY_A not in text
    assert hash_jarvis_key(KEY_A) not in text
    assert "weather" not in text


@pytest.mark.req("REQ-4.2-1")
def test_the_snapshot_is_only_ever_asked_about_the_digest_never_the_raw_key() -> None:
    asked: list[str] = []

    class Spy(Snapshot):
        def key(self, key_hash: str) -> KeyEntry | None:
            asked.append(key_hash)
            return super().key(key_hash)

    base = _snap([_api("a1", "p1", "weather")], {KEY_A: _key("k1", "p1")})
    spy = Spy(base.apis, base.keys, base.source, base.synced_at)
    authenticate(spy, "weather", _hdr(KEY_A))
    assert asked == [hash_jarvis_key(KEY_A)]


def test_forward_headers_drops_the_key_header_and_the_slot_in_every_case() -> None:
    snap = _snap([_api("a1", "p1", "weather")], {KEY_A: _key("k1", "p1")})
    headers = [
        ("Accept", "*/*"),
        ("X-Jarvis-Key", KEY_A),
        ("Authorization", "Bearer something-else"),
        ("X-Other", "1"),
    ]
    got = authenticate(snap, "weather", headers)
    assert got.forward_headers(headers) == [("Accept", "*/*"), ("X-Other", "1")]


def test_authenticated_repr_shows_ids_only() -> None:
    snap = _snap([_api("a1", "p1", "weather")], {KEY_A: _key("k1", "p1")})
    text = repr(authenticate(snap, "weather", _hdr(KEY_A)))
    assert KEY_A not in text
    assert "k1" in text
    assert "a1" in text
