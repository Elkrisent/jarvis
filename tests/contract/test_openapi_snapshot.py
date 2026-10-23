"""OpenAPI contract: the committed snapshot must match what the app generates."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_PROPERTIES = {"secret_ref", "hash", "password"}


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "openapi_snapshot", ROOT / "scripts" / "openapi_snapshot.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# No ``contract`` marker exists in pyproject.toml, so this runs under ``unit`` (no I/O at all).
@pytest.mark.unit
def test_openapi_matches_snapshot() -> None:
    script = _load_script()
    snapshot: Path = script.SNAPSHOT
    assert snapshot.exists(), "snapshot missing: run `make openapi-snapshot`"
    diff = script.diff_against_snapshot()
    assert not diff, (
        "The OpenAPI contract changed. Run `make openapi-snapshot` and explain the change "
        f"in the PR description.\n{diff}"
    )


def _property_names(node: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            names.update(props)
        for value in node.values():
            names |= _property_names(value)
    elif isinstance(node, list):
        for item in node:
            names |= _property_names(item)
    return names


@pytest.mark.unit
def test_snapshot_has_no_secret_property_names() -> None:
    schemas = json.loads((ROOT / "tests/contract/openapi.snapshot.json").read_text())
    assert schemas["paths"], "snapshot looks empty"
    assert _property_names(schemas) & FORBIDDEN_PROPERTIES == set()
