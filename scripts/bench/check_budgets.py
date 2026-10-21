#!/usr/bin/env python3
"""Compare benchmark results with benchmarks/budgets.yaml (IMPLEMENTATION §C.3).

Prints (and appends to $GITHUB_STEP_SUMMARY) a table: metric, value, budget, status, change
vs a baseline. On GitHub-hosted runners absolute budgets are reported but not enforced; pass
``--enforce`` (as ``make bench-checkpoint`` does) to fail on a violation.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from benchlib import Budget, judge, load_budgets, read_json

ROOT = Path(__file__).resolve().parents[2]


def build_rows(
    results: dict[str, float], budgets: dict[str, Budget], baseline: dict[str, float] | None
) -> list[dict[str, str]]:
    rows = []
    for name in sorted(results):
        value = results[name]
        budget = budgets.get(name)
        status = judge(value, budget)
        limit = "—"
        if budget and budget.budget is not None:
            limit = f"{'≤' if budget.direction == 'lower' else '≥'} {budget.budget:g}"
        delta = "—"
        if baseline and name in baseline and baseline[name]:
            delta = f"{(value - baseline[name]) / baseline[name] * 100:+.1f}%"
        rows.append(
            {
                "metric": name,
                "value": f"{value:.4g}",
                "unit": budget.unit if budget else "",
                "budget": limit,
                "status": status,
                "delta": delta,
            }
        )
    return rows


def render(rows: list[dict[str, str]]) -> str:
    lines = [
        "| Metric | Value | Unit | Budget | Status | Δ vs main |",
        "|--------|-------|------|--------|--------|-----------|",
    ]
    for r in rows:
        cells = (r["metric"], r["value"], r["unit"], r["budget"], r["status"], r["delta"])
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("results", type=Path, nargs="+", help="flat {metric: value} JSON files")
    ap.add_argument("--budgets", type=Path, default=ROOT / "benchmarks" / "budgets.yaml")
    ap.add_argument("--baseline", type=Path, help="results from main, for the change column")
    ap.add_argument("--enforce", action="store_true", help="exit 1 when a budget is exceeded")
    args = ap.parse_args(argv)

    results: dict[str, float] = {}
    for path in args.results:
        results.update(read_json(path))
    baseline = read_json(args.baseline) if args.baseline and args.baseline.exists() else None
    rows = build_rows(results, load_budgets(args.budgets), baseline)
    table = render(rows)

    print(table)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            fh.write("### Benchmarks\n\n" + table + "\n")
    over = [r for r in rows if r["status"] == "over budget"]
    unknown = [r for r in rows if r["status"] == "unknown metric"]
    for r in unknown:
        print(f"check_budgets: metric {r['metric']!r} is not in budgets.yaml", file=sys.stderr)
    for r in over:
        print(
            f"check_budgets: {r['metric']} = {r['value']} violates {r['budget']}", file=sys.stderr
        )
    if unknown or (over and args.enforce):
        return 1
    if over:
        print("check_budgets: budgets exceeded but not enforced (no --enforce)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
