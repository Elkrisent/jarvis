#!/usr/bin/env bash
# Numbers of record for a phase: micro benchmarks, idle footprint, k6 scenarios, env.json,
# then benchmarks/checkpoints/P<n>.json + BENCHMARKS.md, and an ENFORCED budget check.
#   run_checkpoint.sh <phase>
set -euo pipefail

phase=${1:?usage: run_checkpoint.sh <phase 0-7>}
dir=build/bench
compose=(docker compose -p jarvis-test --profile test -f deploy/docker-compose.yml
  -f deploy/docker-compose.test.yml --env-file deploy/.env.example)

rm -rf "$dir"
mkdir -p "$dir/k6"
trap '"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true' EXIT

uv run pytest -m perf packages/core/tests/bench --benchmark-json="$dir/micro.json" -q
uv run python scripts/bench/micro_results.py "$dir/micro.json" --results "$dir/results.json"

"${compose[@]}" up -d --build --wait
# Idle footprint of the default profile: the mock upstream (test profile) is excluded.
uv run python scripts/bench/docker_stats.py sample --duration 30 --interval 5 --out "$dir/stats.jsonl"
uv run python scripts/bench/docker_stats.py summarize "$dir/stats.jsonl" --match jarvis-test- \
  --exclude mock-upstream --results "$dir/results.json" --total-metric stack_idle_rss_total_mb

make bench-load

uv run python scripts/bench/write_env.py --out "$dir/env.json" --profile default \
  --param k6_duration=10s --param k6_vus=10
uv run python scripts/bench/checkpoint.py --phase "$phase" --results "$dir/results.json" --env "$dir/env.json" \
  --raw "micro=$dir/micro.json" --raw "k6_dry_run=$dir/k6/dry_run.json"
uv run python scripts/bench/check_budgets.py "$dir/results.json" --enforce
