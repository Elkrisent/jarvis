#!/usr/bin/env bash
# S8: the S5 mixed workload while Redis is killed and restored. The error rate caused by JARVIS
# stays 0, p99 during the outage is at most 2x the normal p99, and the stack is back to normal
# within 5 s of Redis returning. Needs the compose test stack up. Records redis_outage_errors,
# redis_outage_recovery_s, redis_normal_p99_ms, redis_outage_p99_ms and redis_outage_p99_ratio in
# $BENCH_DIR/results.json.
#   run_s8.sh [BENCH_DIR]
set -euo pipefail

dir=${1:-${BENCH_DIR:-build/bench}}
mkdir -p "$dir/k6"
export BENCH_DIR=$dir
rate=${RATE:-100}
compose=(docker compose -p jarvis-test --profile test -f deploy/docker-compose.yml
  -f deploy/docker-compose.test.yml --env-file deploy/.env.example)
mock=${MOCK_URL:-http://127.0.0.1:19000}
gateway=${GATEWAY_URL:-http://127.0.0.1:18080}

rules='[{"path_prefix": "/missing", "status": 404, "latency_range_ms": [20, 60]},
        {"path_prefix": "/", "latency_range_ms": [20, 60]}]'
big='{"enabled": true, "capacity": 1000000, "refill_per_s": 1000000}'
# Both limiters on (with limits nobody reaches) so the Redis path is exercised on every request.
api="{\"rate_limits\": {\"inbound\": $big, \"upstream\": $big}}"
key=$(uv run python scripts/bench/scenario_setup.py setup --slug s8 --rules "$rules" --api "$api")
cleanup() {
  "${compose[@]}" start redis >/dev/null 2>&1 || true
  uv run python scripts/bench/scenario_setup.py teardown --slug s8 >/dev/null 2>&1 || true
}
trap cleanup EXIT

K6_EXTRA_ARGS="--out json=/out/s8_points.json" scripts/bench/run_k6.sh \
  tests/load/scenarios/s5_mixed.js "$dir/k6/s8.json" \
  TARGET=http://gateway:8080/gw/s8 "KEY=$key" "RATE=$rate" DURATION=45s &
k6_pid=$!

sleep 12                                              # normal traffic
curl -s -o /dev/null "$mock/__marker/kill"            # stamps the time on the mock's clock
"${compose[@]}" stop redis >/dev/null                 # Redis killed under load
sleep 12                                              # outage
curl -s -o /dev/null "$mock/__marker/restore"
"${compose[@]}" start redis >/dev/null                # Redis restored
until "${compose[@]}" exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; do sleep 0.1; done
curl -s -o /dev/null "$mock/__marker/redis_up"        # Redis answers: the recovery clock starts
wait "$k6_pid"

# The gateway logs "Redis is back after N s" the moment its health tracker recovers.
"${compose[@]}" logs --timestamps --no-log-prefix gateway 2>&1 | grep "Redis is back" | tail -1 \
  >"$dir/k6/s8_recovery_log.txt"
uv run python scripts/bench/s8_results.py "$dir/k6/s8.json" "$dir/k6/s8_points.json" \
  --recovery-log "$dir/k6/s8_recovery_log.txt" --results "$dir/results.json"
