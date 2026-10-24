#!/usr/bin/env python3
"""Merge several runs of the same metrics into results.json as per-metric medians.

  median_runs.py --out results.json --runs-out runs.json run1.json run2.json ...
Each run file is {metric: value}. The medians are merged into --out (other metrics are kept);
all raw values are written to --runs-out.
"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

from benchlib import read_json, write_json


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--runs-out", type=Path, required=True)
    args = ap.parse_args(argv)

    runs = [read_json(path) for path in args.runs]
    names = sorted(set().union(*runs))
    raw = {name: [run[name] for run in runs if name in run] for name in names}
    medians = {name: statistics.median(values) for name, values in raw.items()}
    current = read_json(args.out) if args.out.exists() else {}
    current.update(medians)
    write_json(args.out, current)
    write_json(args.runs_out, raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
