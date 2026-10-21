#!/usr/bin/env python3
"""Convert a k6 summary into github-action-benchmark JSON (IMPLEMENTATION §C.3).

Latencies go to a ``customSmallerIsBetter`` file and throughput to a ``customBiggerIsBetter``
file. Existing files are extended, so several scenarios can be added one after another.
Also writes ``--results`` (a flat ``{metric: value}`` JSON) for the budget checker.

    k6_to_gab.py summary.json --prefix mock_direct --smaller out/smaller.json \\
        --bigger out/bigger.json --results out/results.json
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from benchlib import k6_trend, read_json, write_json


def convert(
    summary: dict[str, Any], prefix: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trend = k6_trend(summary)
    smaller = [
        {"name": f"{prefix}_p50", "unit": "ms", "value": trend["med"]},
        {"name": f"{prefix}_p95", "unit": "ms", "value": trend["p(95)"]},
        {"name": f"{prefix}_p99", "unit": "ms", "value": trend["p(99)"]},
    ]
    bigger = [
        {"name": f"{prefix}_rps", "unit": "req/s", "value": summary["metrics"]["http_reqs"]["rate"]}
    ]
    return smaller, bigger


def _merge(path: Path, items: list[dict[str, Any]]) -> None:
    existing: list[dict[str, Any]] = read_json(path) if path.exists() else []
    names = {i["name"] for i in items}
    write_json(path, [e for e in existing if e["name"] not in names] + items)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("summary", type=Path)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--smaller", type=Path, required=True)
    ap.add_argument("--bigger", type=Path, required=True)
    ap.add_argument("--results", type=Path)
    args = ap.parse_args(argv)

    smaller, bigger = convert(read_json(args.summary), args.prefix)
    _merge(args.smaller, smaller)
    _merge(args.bigger, bigger)
    if args.results:
        current = read_json(args.results) if args.results.exists() else {}
        current.update({i["name"]: i["value"] for i in smaller + bigger})
        write_json(args.results, current)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
