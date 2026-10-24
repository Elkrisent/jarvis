"""Write or check the telemetry schema snapshot.

Usage: ``python scripts/telemetry_snapshot.py [--write]``. Without ``--write`` it exits 1 when the
committed snapshot is stale.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jarvis_core.telemetry import snapshot_document

SNAPSHOT = Path(__file__).resolve().parents[1] / "tests" / "contract" / "telemetry.snapshot.json"


def render() -> str:
    return json.dumps(snapshot_document(), indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.write:
        SNAPSHOT.write_text(render())
        print(f"wrote {SNAPSHOT}")
        return 0
    if not SNAPSHOT.exists() or SNAPSHOT.read_text() != render():
        print("telemetry snapshot is stale: bump SCHEMA_VERSION and run `make telemetry-snapshot`")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
