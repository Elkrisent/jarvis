#!/usr/bin/env bash
# Run a k6 scenario in the pinned k6 image, attached to the compose test network.
#   run_k6.sh <scenario.js under tests/load> <summary-out.json> [KEY=VALUE ...]
# Env: K6_IMAGE, NETWORK (default jarvis-test_default), TARGET (default the mock upstream),
# K6_EXTRA_ARGS (extra k6 flags, e.g. --out json=/out/points.json).
set -euo pipefail

scenario=$1
out=$2
shift 2

K6_IMAGE=${K6_IMAGE:-grafana/k6:0.55.0}
NETWORK=${NETWORK:-jarvis-test_default}
TARGET=${TARGET:-http://mock-upstream:8080}

out_dir=$(cd "$(dirname "$out")" && pwd)
chmod 777 "$out_dir" # the k6 image runs as a non-root user

env_args=(-e "TARGET=$TARGET")
for kv in "$@"; do env_args+=(-e "$kv"); done

docker run --rm --network "$NETWORK" \
  -v "$PWD/tests/load:/load:ro" -v "$out_dir:/out" \
  "${env_args[@]}" "$K6_IMAGE" run \
  --summary-export "/out/$(basename "$out")" ${K6_EXTRA_ARGS:-} "/load/${scenario#tests/load/}"
