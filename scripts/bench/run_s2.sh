#!/usr/bin/env bash
# S2 pass-through overhead (gw_overhead_miss_p50/p99): the same k6 scenario against the mock
# upstream directly and through the gateway; overhead = proxied - direct per percentile.
# A single p99 on a shared machine swings by several milliseconds, so the pair is run REPEATS
# times (default 3) and the median of each metric is recorded. The raw per-run values are kept in
# $BENCH_DIR/s2_runs.json.
# Needs the compose test stack up (the checkpoint script brings up everything). Results merge into
# $BENCH_DIR/results.json.
#   run_s2.sh [BENCH_DIR]
set -euo pipefail

dir=${1:-${BENCH_DIR:-build/bench}}
mkdir -p "$dir/k6"
export BENCH_DIR=$dir
rate=${RATE:-100}        # requests per second (open model); fixed in advance, see s2_passthrough.js
duration=${DURATION:-30s}
repeats=${REPEATS:-3}

key=$(uv run python scripts/bench/s2_setup.py setup)
trap 'uv run python scripts/bench/s2_setup.py teardown >/dev/null 2>&1 || true' EXIT

k6() { # k6 <target> <summary.json> [extra KEY=VALUE ...]
  local target=$1 out=$2
  shift 2
  scripts/bench/run_k6.sh tests/load/scenarios/s2_passthrough.js "$out" "TARGET=$target" "$@"
}

# Warm both paths once (connections, imports), then measure.
k6 http://mock-upstream:8080 "$dir/k6/s2_warm.json" RATE=20 DURATION=3s >/dev/null
k6 http://gateway:8080/gw/s2 "$dir/k6/s2_warm.json" RATE=20 DURATION=3s "KEY=$key" >/dev/null

runs=()
for i in $(seq 1 "$repeats"); do
  k6 http://mock-upstream:8080 "$dir/k6/s2_direct_$i.json" "RATE=$rate" "DURATION=$duration"
  k6 http://gateway:8080/gw/s2 "$dir/k6/s2_proxied_$i.json" "RATE=$rate" "DURATION=$duration" "KEY=$key"
  uv run python scripts/bench/overhead.py "$dir/k6/s2_direct_$i.json" "$dir/k6/s2_proxied_$i.json" \
    --prefix gw_overhead_miss --results "$dir/s2_run_$i.json"
  runs+=("$dir/s2_run_$i.json")
done

uv run python scripts/bench/median_runs.py --out "$dir/results.json" --runs-out "$dir/s2_runs.json" "${runs[@]}"
