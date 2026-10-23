"""Immutable snapshot of every API policy and JARVIS key the gateway serves from."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, cast

from jarvis_core.config import ApiDependency
from pydantic import ValidationError

Source = Literal["postgres", "file"]
FORMAT = 1
_SOURCES = ("postgres", "file")
_MAX_VERSION = 2**62
_NO_APIS: Mapping[str, ApiEntry | None] = MappingProxyType({})
_NO_KEYS: Mapping[str, KeyEntry | None] = MappingProxyType({})


@dataclass(frozen=True)
class ApiEntry:
    api_id: str
    project_id: str
    slug: str
    version: int
    config: ApiDependency

    def __repr__(self) -> str:
        return f"ApiEntry(api_id={self.api_id!r}, version={self.version})"


@dataclass(frozen=True)
class KeyEntry:
    key_id: str
    project_id: str
    allowed_api_ids: frozenset[str]
    revoked: bool

    def __repr__(self) -> str:
        return f"KeyEntry(key_id={self.key_id!r}, revoked={self.revoked})"


@dataclass(frozen=True, repr=False)
class Snapshot:
    """Read-only config. Build a new one with ``with_changes``; never mutate."""

    apis: Mapping[str, ApiEntry]  # by api_id
    keys: Mapping[str, KeyEntry]  # by key hash
    source: Source
    synced_at: float
    _by_slug: Mapping[tuple[str, str], ApiEntry] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        apis = dict(self.apis)
        object.__setattr__(self, "apis", MappingProxyType(apis))
        object.__setattr__(self, "keys", MappingProxyType(dict(self.keys)))
        by_slug = {(a.project_id, a.slug): a for a in apis.values()}
        object.__setattr__(self, "_by_slug", MappingProxyType(by_slug))

    def __repr__(self) -> str:
        return (
            f"Snapshot(apis={len(self.apis)}, keys={len(self.keys)}, "
            f"source={self.source!r}, synced_at={self.synced_at})"
        )

    @classmethod
    def empty(cls, source: Source, synced_at: float) -> Snapshot:
        return cls({}, {}, source, synced_at)

    def api(self, project_id: str, slug: str) -> ApiEntry | None:
        return self._by_slug.get((project_id, slug))

    def api_by_id(self, api_id: str) -> ApiEntry | None:
        return self.apis.get(api_id)

    def key(self, key_hash: str) -> KeyEntry | None:
        return self.keys.get(key_hash)

    def with_changes(
        self,
        *,
        apis: Mapping[str, ApiEntry | None] = _NO_APIS,
        keys: Mapping[str, KeyEntry | None] = _NO_KEYS,
        remove_project: str | None = None,
        source: Source | None = None,
        synced_at: float | None = None,
    ) -> Snapshot:
        """Return a new snapshot. ``None`` removes; an older API version is ignored.

        ``remove_project`` is applied first, before the adds in the same call, so
        entries added in this call survive even for the removed project.
        """
        for api_id, entry in apis.items():
            if entry is not None and entry.api_id != api_id:
                raise ValueError(f"apis mapping key {api_id} does not match entry api_id")
        new_apis = dict(self.apis)
        new_keys = dict(self.keys)
        if remove_project is not None:
            new_apis = {k: v for k, v in new_apis.items() if v.project_id != remove_project}
            new_keys = {k: v for k, v in new_keys.items() if v.project_id != remove_project}
        for api_id, entry in apis.items():
            if entry is None:
                new_apis.pop(api_id, None)
                continue
            held = new_apis.get(api_id)
            if held is not None and entry.version < held.version:
                continue
            new_apis[api_id] = entry
        for key_hash, key_entry in keys.items():
            if key_entry is None:
                new_keys.pop(key_hash, None)
            else:
                new_keys[key_hash] = key_entry
        return Snapshot(
            new_apis,
            new_keys,
            self.source if source is None else source,
            self.synced_at if synced_at is None else synced_at,
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "format": FORMAT,
            "source": self.source,
            "synced_at": self.synced_at,
            "apis": [
                {
                    "api_id": a.api_id,
                    "project_id": a.project_id,
                    "slug": a.slug,
                    "version": a.version,
                    "config": a.config.model_dump(mode="json"),
                }
                for a in self.apis.values()
            ],
            "keys": [
                {
                    "key_hash": h,
                    "key_id": k.key_id,
                    "project_id": k.project_id,
                    "allowed_api_ids": sorted(k.allowed_api_ids),
                    "revoked": k.revoked,
                }
                for h, k in self.keys.items()
            ],
        }

    @classmethod
    def from_json_dict(cls, data: object) -> Snapshot:
        """Parse ``to_json_dict`` output. Errors never include config contents."""
        if not isinstance(data, dict):
            raise ValueError("snapshot is not an object")
        if data.get("format") != FORMAT:
            raise ValueError("unsupported snapshot format")
        source = data.get("source")
        synced_at = data.get("synced_at")
        if source not in _SOURCES:
            raise ValueError("invalid snapshot source")
        synced = _finite(synced_at)
        if synced is None:
            raise ValueError("invalid snapshot synced_at")
        raw_apis, raw_keys = data.get("apis"), data.get("keys")
        if not isinstance(raw_apis, list) or not isinstance(raw_keys, list):
            raise ValueError("snapshot apis/keys missing or not lists")
        apis: dict[str, ApiEntry] = {}
        slugs: set[tuple[str, str]] = set()
        for raw in raw_apis:
            entry = _parse_api(raw)
            if entry.api_id in apis:
                raise ValueError(f"duplicate api_id {entry.api_id}")
            if (entry.project_id, entry.slug) in slugs:
                raise ValueError(f"duplicate slug for project in api {entry.api_id}")
            apis[entry.api_id] = entry
            slugs.add((entry.project_id, entry.slug))
        keys: dict[str, KeyEntry] = {}
        for raw in raw_keys:
            key_hash, key_entry = _parse_key(raw)
            if key_hash in keys:
                raise ValueError(f"duplicate key hash for key {key_entry.key_id}")
            keys[key_hash] = key_entry
        return cls(apis, keys, cast(Source, source), synced)


def _finite(value: object) -> float | None:
    """``value`` as a finite float, or None for bools, non-numbers, NaN/inf and overflow."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _string(raw: dict[str, Any], name: str, what: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{what}: {name} must be a string")
    return value


def _parse_api(raw: object) -> ApiEntry:
    if not isinstance(raw, dict):
        raise ValueError("api entry is not an object")
    api_id = _string(raw, "api_id", "api entry")
    what = f"api {api_id}"
    project_id = _string(raw, "project_id", what)
    slug = _string(raw, "slug", what)
    version = raw.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or not 1 <= version <= _MAX_VERSION
    ):
        raise ValueError(f"{what}: version must be a positive integer")
    try:
        config = ApiDependency.model_validate(raw.get("config"))
    except ValidationError:
        # Deliberately drop the pydantic detail: it echoes input values.
        raise ValueError(f"{what}: invalid config") from None
    return ApiEntry(api_id, project_id, slug, version, config)


def _parse_key(raw: object) -> tuple[str, KeyEntry]:
    if not isinstance(raw, dict):
        raise ValueError("key entry is not an object")
    what = "key entry"
    key_hash = _string(raw, "key_hash", what)
    key_id = _string(raw, "key_id", what)
    project_id = _string(raw, "project_id", what)
    allowed = raw.get("allowed_api_ids")
    if not isinstance(allowed, list) or not all(isinstance(a, str) for a in allowed):
        raise ValueError(f"{what}: allowed_api_ids must be a list of strings")
    revoked = raw.get("revoked")
    if not isinstance(revoked, bool):
        raise ValueError(f"{what}: revoked must be a boolean")
    return key_hash, KeyEntry(key_id, project_id, frozenset(allowed), revoked)
