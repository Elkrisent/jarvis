#!/usr/bin/env python3
"""Enforce per-package coverage gates (IMPLEMENTATION §B.3) from a coverage.py JSON report.

Fails when a package is below its line/branch gate, or (with ``--baseline``) when line or
branch coverage dropped by more than 1 point relative to the baseline report.
Writes a markdown table to stdout, and appends it to ``$GITHUB_STEP_SUMMARY`` when set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_DROP = 1.0  # points relative to the baseline


@dataclass(frozen=True)
class Gate:
    name: str
    prefix: str
    line: float
    branch: float


GATES = (
    Gate("packages/core", "packages/core/src/", 95.0, 90.0),
    Gate("services/gateway", "services/gateway/src/", 90.0, 85.0),
    Gate("services/api", "services/api/src/", 85.0, 75.0),
    Gate("services/worker", "services/worker/src/", 85.0, 75.0),
)


@dataclass(frozen=True)
class Measured:
    line: float
    branch: float


def measure(report: dict[str, object], prefix: str) -> Measured | None:
    """Combined line and branch percentage of every file under ``prefix``."""
    files = report["files"]
    assert isinstance(files, dict)
    stmts = covered = branches = covered_branches = 0
    seen = False
    for path, data in files.items():
        if not path.startswith(prefix):
            continue
        seen = True
        s = data["summary"]
        stmts += s["num_statements"]
        covered += s["covered_lines"]
        branches += s.get("num_branches", 0)
        covered_branches += s.get("covered_branches", 0)
    if not seen:
        return None
    return Measured(
        line=100.0 * covered / stmts if stmts else 100.0,
        branch=100.0 * covered_branches / branches if branches else 100.0,
    )


def evaluate(
    report: dict[str, object], baseline: dict[str, object] | None = None
) -> tuple[str, list[str]]:
    rows = [
        "| Package | Line | Gate | Branch | Gate | vs main |",
        "|---------|------|------|--------|------|---------|",
    ]
    problems: list[str] = []
    for gate in GATES:
        now = measure(report, gate.prefix)
        if now is None:
            rows.append(
                f"| {gate.name} | no code | {gate.line:.0f}% | no code | {gate.branch:.0f}% | — |"
            )
            continue
        delta = "—"
        if baseline is not None and (before := measure(baseline, gate.prefix)) is not None:
            d_line, d_branch = now.line - before.line, now.branch - before.branch
            delta = f"{d_line:+.1f} / {d_branch:+.1f}"
            if d_line < -MAX_DROP or d_branch < -MAX_DROP:
                problems.append(
                    f"{gate.name}: coverage dropped more than {MAX_DROP:.0f} point(s) vs main "
                    f"(line {d_line:+.1f}, branch {d_branch:+.1f})"
                )
        if now.line < gate.line:
            problems.append(f"{gate.name}: line coverage {now.line:.1f}% < {gate.line:.0f}%")
        if now.branch < gate.branch:
            problems.append(f"{gate.name}: branch coverage {now.branch:.1f}% < {gate.branch:.0f}%")
        rows.append(
            f"| {gate.name} | {now.line:.1f}% | {gate.line:.0f}% | {now.branch:.1f}% "
            f"| {gate.branch:.0f}% | {delta} |"
        )
    return "\n".join(rows) + "\n", problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report", type=Path, default=Path("build/coverage.json"))
    ap.add_argument("--baseline", type=Path, help="coverage.json from main")
    args = ap.parse_args(argv)

    report = json.loads(args.report.read_text())
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    table, problems = evaluate(report, baseline)

    print(table)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            fh.write("### Coverage\n\n" + table + "\n")
            for p in problems:
                fh.write(f"- :x: {p}\n")
    for p in problems:
        print(f"coverage gate: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
