#!/usr/bin/env bash
# S4 failing upstream (50% 503): the retry budget keeps upstream calls per client request at most
# 1 + budget_ratio, and the circuit opens so most failures are refused without calling the upstream.
# Needs the compose test stack up. Records retry_storm_calls_per_request and s4_circuit_open_ratio
# in $BENCH_DIR/results.json.
#   run_s4.sh [BENCH_DIR]
set -euo pipefail

dir=${1:-${BENCH_DIR:-build/bench}}
mkdir -p "$dir/k6"
export BENCH_DIR=$dir
rate=${RATE:-100}
duration=${DURATION:-30s}

rules='[{"path_prefix": "/", "latency_ms": 20, "errors": [{"status": 503, "rate": 0.5}]}]'
key=$(uv run python scripts/bench/scenario_setup.py setup --slug s4 --rules "$rules")
trap 'uv run python scripts/bench/scenario_setup.py teardown --slug s4 >/dev/null 2>&1 || true' EXIT

scripts/bench/run_k6.sh tests/load/scenarios/s4_failing.js "$dir/k6/s4.json" \
  TARGET=http://gateway:8080/gw/s4 "KEY=$key" "RATE=$rate" "DURATION=$duration"
uv run python scripts/bench/s4_results.py "$dir/k6/s4.json" --results "$dir/results.json"
