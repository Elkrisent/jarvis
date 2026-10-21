#!/usr/bin/env python3
"""Write a checkpoint snapshot and regenerate BENCHMARKS.md (IMPLEMENTATION §C.3).

    checkpoint.py --phase 0 --results build/bench/results.json --env build/bench/env.json \\
        [--raw micro=build/bench/micro.json --raw k6=build/bench/k6-summary.json]

Writes benchmarks/checkpoints/P<n>.json (numbers of record: results, raw data and env.json)
and BENCHMARKS.md with columns: metric · budget · P0 … P7 · status.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from benchlib import Budget, judge, load_budgets, read_json, write_json

ROOT = Path(__file__).resolve().parents[2]
PHASES = range(8)


def load_checkpoints(directory: Path) -> dict[int, dict[str, Any]]:
    out = {}
    for path in sorted(directory.glob("P*.json")):
        if m := re.fullmatch(r"P(\d+)\.json", path.name):
            out[int(m[1])] = read_json(path)
    return out


def render(budgets: dict[str, Budget], checkpoints: dict[int, dict[str, Any]]) -> str:
    seen = {m for cp in checkpoints.values() for m in cp["results"]}
    names = [n for n in budgets if n in seen or budgets[n].budget is not None] + sorted(
        seen - set(budgets)
    )
    header = ["Metric", "Budget", *[f"P{p}" for p in PHASES], "Status"]
    lines = [
        "# Benchmarks",
        "",
        "Numbers of record, written by `make bench-checkpoint PHASE=<n>` on the machine recorded",
        "in each `benchmarks/checkpoints/P<n>.json` (`env`). Hosted CI runs are for trends only.",
        "Budgets come from `benchmarks/budgets.yaml` (SPEC §13).",
        "`—` means not measured at that checkpoint.",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    for name in names:
        b = budgets.get(name)
        limit = "—"
        if b and b.budget is not None:
            limit = f"{'≤' if b.direction == 'lower' else '≥'} {b.budget:g} {b.unit}"
        cells = []
        latest: float | None = None
        for p in PHASES:
            value = checkpoints.get(p, {}).get("results", {}).get(name)
            cells.append("—" if value is None else f"{value:.4g}")
            if value is not None:
                latest = value
        status = "not measured" if latest is None else judge(latest, b)
        lines.append("| " + " | ".join([name, limit, *cells, status]) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--phase", type=int, required=True, choices=list(PHASES))
    ap.add_argument("--results", type=Path, required=True, nargs="+")
    ap.add_argument("--env", type=Path, required=True)
    ap.add_argument("--raw", action="append", default=[], metavar="NAME=PATH")
    args = ap.parse_args(argv)

    results: dict[str, float] = {}
    for path in args.results:
        results.update(read_json(path))
    raw = {name: read_json(Path(path)) for name, path in (r.split("=", 1) for r in args.raw)}
    snapshot = {"phase": args.phase, "env": read_json(args.env), "results": results, "raw": raw}
    write_json(root / "benchmarks" / "checkpoints" / f"P{args.phase}.json", snapshot)

    budgets = load_budgets(root / "benchmarks" / "budgets.yaml")
    text = render(budgets, load_checkpoints(root / "benchmarks" / "checkpoints"))
    (root / "BENCHMARKS.md").write_text(text)
    print(f"checkpoint: wrote benchmarks/checkpoints/P{args.phase}.json and BENCHMARKS.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
