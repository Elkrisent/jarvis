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
includes the HTTP request and is an upper bound on commit-to-store latency.

| Metric | Budget | p50 | p99 | Status |
|---|---|---|---|---|
| config_propagation_p99 | ≤ 1000 ms | 10.8 ms | 20.8 ms | ok |

Three local runs gave p50 10.5 to 10.8 ms and p99 17.7 to 20.8 ms. The table above gets this
metric at the P1 checkpoint (`make bench-checkpoint PHASE=1`), which regenerates this file.
