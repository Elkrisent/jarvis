"""Dump the management API's OpenAPI document and compare it with the committed snapshot.

Usage: ``python scripts/openapi_snapshot.py [--write]``. Without ``--write`` it prints a unified
diff and exits 1 when the snapshot is stale. The app is built with the api tests' fake services,
so no database or Redis is needed.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "contract" / "openapi.snapshot.json"


def generate() -> str:
    # fakes.py is a plain module beside the api tests; reuse it instead of duplicating fakes.
    sys.path.insert(0, str(ROOT / "services" / "api" / "tests"))
    from fakes import ADMIN_TOKEN, fake_services
    from jarvis_api.app import create_app

    app = create_app(services=fake_services(), admin_token=ADMIN_TOKEN)
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def diff_against_snapshot() -> str:
    """Unified diff of the committed snapshot against the generated document ('' when equal)."""
    current = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.exists() else ""
    lines = difflib.unified_diff(
        current.splitlines(keepends=True),
        generate().splitlines(keepends=True),
        fromfile=str(SNAPSHOT.relative_to(ROOT)),
        tofile="generated",
    )
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the snapshot file")
    args = parser.parse_args()
    if args.write:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(generate(), encoding="utf-8")
        print(f"wrote {SNAPSHOT.relative_to(ROOT)}")
        return 0
    diff = diff_against_snapshot()
    if diff:
        sys.stdout.write(diff)
        print("\nOpenAPI snapshot is stale: run `make openapi-snapshot`.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
