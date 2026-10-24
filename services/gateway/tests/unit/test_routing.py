"""Per-API route templaters that follow the config snapshot."""

from __future__ import annotations

from typing import Any

import pytest
from jarvis_core.config import ApiDependency
from jarvis_gateway.config.snapshot import ApiEntry, Snapshot
from jarvis_gateway.routing import TemplateRegistry

pytestmark = pytest.mark.unit


def api(api_id: str, version: int = 1, routes: tuple[str, ...] = ()) -> ApiEntry:
    raw: dict[str, Any] = {
        "slug": api_id,
        "name": api_id,
        "base_url": "https://api.example.com",
        "routes": [{"pattern": p} for p in routes],
    }
    return ApiEntry(api_id, "p1", api_id, version, ApiDependency.model_validate(raw))


def snapshot(*apis: ApiEntry) -> Snapshot:
    return Snapshot.empty("postgres", 1.0).with_changes(apis={a.api_id: a for a in apis})


@pytest.mark.req("REQ-4.14-3")
def test_explicit_patterns_and_the_id_fallback() -> None:
    registry = TemplateRegistry()
    entry = api("a1", routes=("/v1/forecast/{city}",))
    assert registry.route_for(entry, "/v1/forecast/paris") == "/v1/forecast/{city}"
    assert registry.route_for(entry, "/v1/users/42") == "/v1/users/{id}"


def test_one_templater_per_api_version_is_reused() -> None:
    registry = TemplateRegistry()
    entry = api("a1")
    registry.route_for(entry, "/a")
    first = registry.templater_for(entry)
    assert registry.templater_for(entry) is first
    assert registry.templater_for(api("a1", version=2)) is not first


def test_retain_drops_templaters_of_apis_that_are_gone_or_changed() -> None:
    registry = TemplateRegistry()
    keep, gone, changed = api("keep"), api("gone"), api("changed")
    for entry in (keep, gone, changed):
        registry.route_for(entry, "/x")
    kept = registry.templater_for(keep)
    registry.retain(snapshot(keep, api("changed", version=2)))
    assert registry.size == 1  # only "keep"; version 2 of "changed" is built when first used
    assert registry.templater_for(keep) is kept
    registry.route_for(api("changed", version=2), "/x")
    assert registry.size == 2
