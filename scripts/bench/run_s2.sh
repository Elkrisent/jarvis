#!/usr/bin/env bash
# S2 pass-through overhead (gw_overhead_miss_p50/p99): the same k6 scenario against the mock
# upstream directly and through the gateway; overhead = proxied - direct per percentile.
# Needs the compose test stack up (make bench-up brings up only the mock upstream; the checkpoint
# script brings up everything). Results merge into $BENCH_DIR/results.json.
#   run_s2.sh [BENCH_DIR]
set -euo pipefail

dir=${1:-${BENCH_DIR:-build/bench}}
mkdir -p "$dir/k6"
export BENCH_DIR=$dir
export VUS=${VUS:-20} DURATION=${DURATION:-15s}

key=$(uv run python scripts/bench/s2_setup.py setup)
trap 'uv run python scripts/bench/s2_setup.py teardown >/dev/null 2>&1 || true' EXIT

# Warm both paths once (connections, imports), then measure.
TARGET=http://mock-upstream:8080 scripts/bench/run_k6.sh tests/load/scenarios/s2_passthrough.js "$dir/k6/s2_warm.json" VUS=2 DURATION=3s >/dev/null
TARGET=http://gateway:8080/gw/s2 scripts/bench/run_k6.sh tests/load/scenarios/s2_passthrough.js "$dir/k6/s2_warm.json" VUS=2 DURATION=3s "KEY=$key" >/dev/null

TARGET=http://mock-upstream:8080 scripts/bench/run_k6.sh tests/load/scenarios/s2_passthrough.js "$dir/k6/s2_direct.json" "VUS=$VUS" "DURATION=$DURATION"
TARGET=http://gateway:8080/gw/s2 scripts/bench/run_k6.sh tests/load/scenarios/s2_passthrough.js "$dir/k6/s2_proxied.json" "VUS=$VUS" "DURATION=$DURATION" "KEY=$key"

uv run python scripts/bench/overhead.py "$dir/k6/s2_direct.json" "$dir/k6/s2_proxied.json" \
  --prefix gw_overhead_miss --results "$dir/results.json"
