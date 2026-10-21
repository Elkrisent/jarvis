#!/usr/bin/env python3
"""Turn a pytest-benchmark JSON into flat results (microseconds per op) for the budget tools."""

from __future__ import annotations

import argparse
from pathlib import Path

from benchlib import micro_results, read_json, write_json


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("micro", type=Path)
    ap.add_argument("--results", type=Path, required=True)
    args = ap.parse_args(argv)
    current = read_json(args.results) if args.results.exists() else {}
    current.update(micro_results(read_json(args.micro)))
    write_json(args.results, current)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
