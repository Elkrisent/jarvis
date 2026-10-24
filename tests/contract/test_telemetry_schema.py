"""Telemetry contract (IMPLEMENTATION B.5.4): schema changes must bump ``v`` and the snapshot."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jarvis_core.telemetry import EVENT_FIELDS, SCHEMA_VERSION, snapshot_document

SNAPSHOT = Path(__file__).resolve().parent / "telemetry.snapshot.json"
HINT = "Bump SCHEMA_VERSION (jarvis_core.telemetry) and run `make telemetry-snapshot`."


@pytest.mark.unit
@pytest.mark.req("REQ-6.1-5")
def test_snapshot_matches_the_event_schema() -> None:
    assert SNAPSHOT.exists(), f"snapshot missing: run `make telemetry-snapshot`. {HINT}"
    committed = json.loads(SNAPSHOT.read_text())
    current = snapshot_document()
    assert committed == current, f"The telemetry event schema changed. {HINT}"


@pytest.mark.unit
@pytest.mark.req("REQ-6.1-5")
def test_changed_fields_require_a_new_version() -> None:
    committed = json.loads(SNAPSHOT.read_text())
    fields_changed = committed["fields"] != [[n, t] for n, t in EVENT_FIELDS]
    if fields_changed:
        assert committed["v"] != SCHEMA_VERSION, f"Fields changed without a version bump. {HINT}"
