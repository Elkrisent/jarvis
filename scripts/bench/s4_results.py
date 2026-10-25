#!/usr/bin/env python3
"""S4 numbers: upstream calls per client request, and the share refused by the open circuit.

  s4_results.py k6-summary.json --results results.json
The upstream call count comes from the mock upstream's call log (it counts every call it received
since the scenario's reset, including those that were answered 503).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import httpx
from benchlib import read_json, write_json

MOCK_URL = os.environ.get("MOCK_URL", "http://127.0.0.1:19000")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("summary", type=Path)
    ap.add_argument("--results", type=Path, required=True)
    args = ap.parse_args()

    metrics = read_json(args.summary)["metrics"]
    requests = metrics["http_reqs"]["count"]
    upstream_calls = httpx.get(f"{MOCK_URL}/__admin/calls", timeout=10).json()["count"]
    refused = metrics.get("s4_circuit_open", {}).get("count", 0)
    answered = metrics.get("s4_upstream_answers", {}).get("count", 0)
    out = {
        "retry_storm_calls_per_request": round(upstream_calls / requests, 4),
        "s4_circuit_open_ratio": round(refused / requests, 4),
    }
    print(
        f"S4: {requests} requests, {upstream_calls} upstream calls "
        f"({out['retry_storm_calls_per_request']} per request), {answered} answered by the "
        f"upstream, {refused} refused by the open circuit"
    )
    current = read_json(args.results) if args.results.exists() else {}
    current.update(out)
    write_json(args.results, current)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
