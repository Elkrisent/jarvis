#!/usr/bin/env python3
"""Sample container memory with ``docker stats`` for footprint and soak tests.

    docker_stats.py sample --duration 30 --interval 5 --out build/bench/stats.jsonl
    docker_stats.py summarize build/bench/stats.jsonl --match gateway --results results.json

``summarize`` reports the mean total RSS in MB (``stack_idle_rss_total_mb``) and, for a
``--slope-metric``, the least-squares memory growth in MB per hour of the matching containers.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from benchlib import read_json, write_json

_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
}


def parse_bytes(text: str) -> float:
    """``'46.5MiB'`` -> bytes. Raises ValueError on anything else."""
    text = text.strip()
    for i, ch in enumerate(text):
        if not (ch.isdigit() or ch == "."):
            number, unit = text[:i], text[i:].strip().lower()
            break
    else:
        raise ValueError(f"no unit in {text!r}")
    if unit not in _UNITS or not number:
        raise ValueError(f"cannot parse {text!r}")
    return float(number) * _UNITS[unit]


def parse_stats_line(line: str) -> tuple[str, float]:
    """One ``docker stats --format '{{json .}}'`` line -> (container name, used MB)."""
    item = json.loads(line)
    used = item["MemUsage"].split("/")[0]
    return item["Name"], parse_bytes(used) / 1024**2


def sample_once() -> dict[str, float]:
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return dict(parse_stats_line(line) for line in out.splitlines() if line.strip())


def slope_per_hour(points: list[tuple[float, float]]) -> float:
    """Least-squares slope of (seconds, MB) points, in MB per hour."""
    if len(points) < 2:
        return 0.0
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    den = sum((x - mean_x) ** 2 for x, _ in points)
    if den == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / den * 3600


def summarize(
    samples: list[dict[str, Any]], match: str | None = None, exclude: str | None = None
) -> dict[str, float]:
    """Mean total MB and growth per hour, for containers whose name contains ``match`` (if given)
    and does not contain ``exclude`` (if given)."""

    def wanted(name: str) -> bool:
        return (match is None or match in name) and (exclude is None or exclude not in name)

    totals = [(s["ts"], sum(v for k, v in s["containers"].items() if wanted(k))) for s in samples]
    if not totals:
        raise ValueError("no samples")
    return {
        "mean_total_mb": sum(t for _, t in totals) / len(totals),
        "slope_mb_per_hour": slope_per_hour(totals),
    }


def cmd_sample(args: argparse.Namespace) -> int:
    args.out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.duration
    with args.out.open("w") as fh:
        while True:
            fh.write(json.dumps({"ts": time.time(), "containers": sample_once()}) + "\n")
            fh.flush()
            if time.monotonic() + args.interval > deadline:
                return 0
            time.sleep(args.interval)


def cmd_summarize(args: argparse.Namespace) -> int:
    samples = [json.loads(line) for line in args.stats.read_text().splitlines() if line.strip()]
    result = summarize(samples, args.match, args.exclude)
    out: dict[str, float] = {}
    if args.total_metric:
        out[args.total_metric] = result["mean_total_mb"]
    if args.slope_metric:
        out[args.slope_metric] = result["slope_mb_per_hour"]
    current = read_json(args.results) if args.results and args.results.exists() else {}
    current.update(out)
    if args.results:
        write_json(args.results, current)
    print(json.dumps(result, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--duration", type=float, default=30)
    s.add_argument("--interval", type=float, default=5)
    s.add_argument("--out", type=Path, required=True)
    s.set_defaults(fn=cmd_sample)
    m = sub.add_parser("summarize")
    m.add_argument("stats", type=Path)
    m.add_argument("--match")
    m.add_argument("--exclude")
    m.add_argument("--results", type=Path)
    m.add_argument("--total-metric")
    m.add_argument("--slope-metric")
    m.set_defaults(fn=cmd_summarize)
    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
