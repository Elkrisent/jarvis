#!/usr/bin/env python3
"""S8 numbers from the k6 run: errors, p99 before and during the Redis outage, recovery time.

  s8_results.py SUMMARY.json POINTS.json --recovery-log LOG-LINE.txt --results results.json
The kill, restore and "Redis answers" moments are the times the mock upstream stamped when the
runner called its /__marker/kill, /restore and /redis_up: the mock, k6 and the gateway run in
containers that share a clock, which the host's clock need not match.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import httpx
from benchlib import read_json, write_json

MOCK_URL = os.environ.get("MOCK_URL", "http://127.0.0.1:19000")
SETTLE_S = 0.3  # requests in flight at the kill are not "during the outage" yet


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


def marker_times() -> dict[str, float]:
    calls = httpx.get(f"{MOCK_URL}/__admin/calls", params={"path_prefix": "/__marker"}, timeout=10)
    return {c["path"].rsplit("/", 1)[-1]: float(c["received_at"]) for c in calls.json()["calls"]}


def recovery_seconds(log_line: str, redis_up: float) -> float:
    """Seconds from Redis answering PING to the gateway's "Redis is back" log line.

    The line starts with Docker's RFC 3339 timestamp (nanoseconds, UTC)."""
    stamp = log_line.split(" ", 1)[0]
    if not stamp:
        raise SystemExit("the gateway never logged that Redis was back")
    head, _, frac = stamp.rstrip("Z").partition(".")
    moment = datetime.fromisoformat(f"{head}.{frac[:6].ljust(6, '0')}+00:00")
    return moment.timestamp() - redis_up


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("summary", type=Path)
    ap.add_argument("points", type=Path)
    ap.add_argument("--recovery-log", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True)
    args = ap.parse_args()

    markers = marker_times()
    killed, restored, redis_up = markers["kill"], markers["restore"], markers["redis_up"]
    recovery_s = recovery_seconds(args.recovery_log.read_text(), redis_up)
    normal: list[float] = []
    outage: list[float] = []
    with args.points.open() as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("type") != "Point" or row.get("metric") != "http_req_duration":
                continue
            at = datetime.fromisoformat(row["data"]["time"]).timestamp()
            ms = float(row["data"]["value"])
            if at < killed:
                normal.append(ms)
            elif killed + SETTLE_S <= at < restored:
                outage.append(ms)
    summary = read_json(args.summary)["metrics"]
    errors = summary.get("s5_unexpected", {}).get("count", 0)
    requests = summary["http_reqs"]["count"]
    normal_p99, outage_p99 = percentile(normal, 99), percentile(outage, 99)
    out = {
        "redis_outage_errors": float(errors),
        "redis_outage_recovery_s": round(recovery_s, 3),
        "redis_normal_p99_ms": round(normal_p99, 3),
        "redis_outage_p99_ms": round(outage_p99, 3),
        "redis_outage_p99_ratio": round(outage_p99 / normal_p99, 3),
    }
    print(
        f"S8: {requests} requests ({len(normal)} before, {len(outage)} during the outage), "
        f"{errors} unexpected answers, p99 {normal_p99:.1f} ms -> {outage_p99:.1f} ms "
        f"(x{out['redis_outage_p99_ratio']}), recovered {recovery_s:.2f} s after Redis "
        "answered PING"
    )
    current = read_json(args.results) if args.results.exists() else {}
    current.update(out)
    write_json(args.results, current)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
