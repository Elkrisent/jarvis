#!/usr/bin/env python3
"""Gateway overhead = latency through JARVIS minus latency direct to the upstream (§C.1).

Run the same k6 scenario twice, once against the mock directly and once through the gateway,
then:  overhead.py direct.json proxied.json --prefix gw_overhead_hit --results results.json
Overhead is computed per percentile (p50 and p99). Throughput is taken from the proxied run
when ``--rps-metric`` is given.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from benchlib import k6_trend, read_json, write_json


def overhead(direct: dict[str, Any], proxied: dict[str, Any], prefix: str) -> dict[str, float]:
    d, p = k6_trend(direct), k6_trend(proxied)
    return {
        f"{prefix}_p50": p["med"] - d["med"],
        f"{prefix}_p99": p["p(99)"] - d["p(99)"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("direct", type=Path)
    ap.add_argument("proxied", type=Path)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--rps-metric", help="also record the proxied run's req/s under this name")
    args = ap.parse_args(argv)

    proxied = read_json(args.proxied)
    out = overhead(read_json(args.direct), proxied, args.prefix)
    if args.rps_metric:
        out[args.rps_metric] = proxied["metrics"]["http_reqs"]["rate"]
    current = read_json(args.results) if args.results.exists() else {}
    current.update(out)
    write_json(args.results, current)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
