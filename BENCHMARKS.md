# Benchmarks

Numbers of record, written by `make bench-checkpoint PHASE=<n>` on the machine recorded
in each `benchmarks/checkpoints/P<n>.json` (`env`). Hosted CI runs are for trends only.
Budgets come from `benchmarks/budgets.yaml` (SPEC §13).
`—` means not measured at that checkpoint.

| Metric | Budget | P0 | P1 | P2 | P3 | P4 | P5 | P6 | P7 | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| gw_overhead_hit_p50 | ≤ 2 ms | — | — | — | — | — | — | — | — | not measured |
| gw_overhead_hit_p99 | ≤ 10 ms | — | — | — | — | — | — | — | — | not measured |
| gw_overhead_miss_p50 | ≤ 3 ms | — | — | — | — | — | — | — | — | not measured |
| gw_overhead_miss_p99 | ≤ 15 ms | — | — | — | — | — | — | — | — | not measured |
| gw_throughput_hit_rps | ≥ 2000 req/s | — | — | — | — | — | — | — | — | not measured |
| gw_redis_roundtrips_pre | ≤ 2 count/req | — | — | — | — | — | — | — | — | not measured |
| gw_redis_roundtrips_post | ≤ 1 count/req | — | — | — | — | — | — | — | — | not measured |
| config_propagation_p99 | ≤ 1000 ms | — | — | — | — | — | — | — | — | not measured |
| stack_idle_rss_total_mb | ≤ 1536 MB | 210.1 | — | — | — | — | — | — | — | ok |
| ingest_lag_p99 | ≤ 2000 ms | — | — | — | — | — | — | — | — | not measured |
| dash_api_p95_overview | ≤ 200 ms | — | — | — | — | — | — | — | — | not measured |
| dash_api_p95_api_detail | ≤ 200 ms | — | — | — | — | — | — | — | — | not measured |
| web_tti | ≤ 1500 ms | — | — | — | — | — | — | — | — | not measured |
| web_perf_score | ≥ 90 score | — | — | — | — | — | — | — | — | not measured |
| web_a11y_score | ≥ 95 score | — | — | — | — | — | — | — | — | not measured |
| token_bucket_take | — | 1.718 | — | — | — | — | — | — | — | no budget |
| mock_direct_p50 | — | 5.393 | — | — | — | — | — | — | — | no budget |
| mock_direct_p95 | — | 10.31 | — | — | — | — | — | — | — | no budget |
| mock_direct_p99 | — | 14.38 | — | — | — | — | — | — | — | no budget |
| mock_direct_rps | — | 1591 | — | — | — | — | — | — | — | no budget |

## Interim measurements (not a checkpoint)

Taken during M1.4 with `make bench-propagation`, locally (not CI): WSL2 on a 12th Gen Intel Core
i5-1235U (12 threads, 7 GB RAM), Postgres and Redis on the same machine, real management app plus
real gateway `ConfigManager` in one process. 200 API PATCHes spaced 20 ms apart; each sample runs
from just before the request is sent to the moment the gateway store shows the new version, so it
includes the HTTP request and is an upper bound on commit-to-store latency. The HTTP part runs over an
in-process ASGI transport (no network hop), so the bound holds for the in-process path only.
The `bench` workflow's first run on a GitHub-hosted runner (main at `b17187d`) recorded p99 15.3 ms.

| Metric | Budget | p50 | p99 | Status |
|---|---|---|---|---|
| config_propagation_p99 | ≤ 1000 ms | 10.8 ms | 20.8 ms | ok |

Three local runs gave p50 10.5 to 10.8 ms and p99 17.7 to 20.8 ms. The table above gets this
metric only once the checkpoint script runs the propagation benchmark. It does not yet:
`scripts/bench/run_checkpoint.sh` skips `make bench-propagation` (which needs `DATABASE_URL` and
`REDIS_URL`), and the next `make bench-checkpoint` regenerates this file and drops this section.

## Interim measurements: telemetry overhead (M1.9, not a checkpoint)

Taken with `make bench-telemetry`, locally (not CI): same WSL2 machine as above, Redis on the same
machine. The real gateway app runs in process over ASGI against a stub upstream that answers after
1 ms. Three arms take turns in blocks of 100 requests: telemetry off (no-op sink), on
(`RedisTelemetrySink` writing to Redis) and Redis unreachable. Each round is 800 requests per arm;
the reported overhead is the median over the rounds of `p99(arm) - p99(off)`. The budget is the
M1.9 Definition of Done: under 0.5 ms.

| Run | Rounds | p99 off | Overhead, telemetry on | Overhead, Redis down |
|---|---|---|---|---|
| 1 | 5 | 3.03 ms | 0.26 ms | -0.09 ms |
| 2 | 5 | 3.22 ms | 0.08 ms | 0.09 ms |
| 3 | 5 | 2.96 ms | 0.31 ms | 0.13 ms |
| 4 | 9 | 3.12 ms | 0.11 ms | 0.16 ms |
| 5 | 9 | 2.90 ms | 0.12 ms | 0.05 ms |
| 6 | 9 | 3.07 ms | 0.38 ms | 0.03 ms |

All six runs passed. The per-round differences swing by about ±1 ms on this machine (a single
round can read negative), so one run's median can land anywhere between roughly 0.1 and 0.4 ms;
treat the budget margin as thin on a noisy runner. The test uses 9 rounds for that reason. A
separate check measures `emit()` alone: p50 0.1 us, p99 about 0.5 us over 50,000 calls (limit 20 us).
Hosted-runner numbers come from the `telemetry` job in `bench.yml` once it has run on `main`.
