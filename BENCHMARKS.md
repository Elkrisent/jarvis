# Benchmarks

Numbers of record, written by `make bench-checkpoint PHASE=<n>` on the machine recorded
in each `benchmarks/checkpoints/P<n>.json` (`env`). Hosted CI runs are for trends only.
Budgets come from `benchmarks/budgets.yaml` (SPEC §13).
`—` means not measured at that checkpoint.

| Metric | Budget | P0 | P1 | P2 | P3 | P4 | P5 | P6 | P7 | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| gw_overhead_hit_p50 | ≤ 2 ms | — | — | — | — | — | — | — | — | not measured |
| gw_overhead_hit_p99 | ≤ 10 ms | — | — | — | — | — | — | — | — | not measured |
| gw_overhead_miss_p50 | ≤ 3 ms | — | 2.722 | — | — | — | — | — | — | ok |
| gw_overhead_miss_p99 | ≤ 15 ms | — | 29.33 | — | — | — | — | — | — | deferred |
| gw_throughput_hit_rps | ≥ 2000 req/s | — | — | — | — | — | — | — | — | not measured |
| gw_redis_roundtrips_pre | ≤ 2 count/req | — | — | — | — | — | — | — | — | not measured |
| gw_redis_roundtrips_post | ≤ 1 count/req | — | — | — | — | — | — | — | — | not measured |
| telemetry_overhead_p99 | ≤ 0.5 ms | — | -0.486 | — | — | — | — | — | — | ok |
| telemetry_overhead_p99_redis_down | ≤ 0.5 ms | — | -0.632 | — | — | — | — | — | — | ok |
| config_propagation_p99 | ≤ 1000 ms | — | 24.93 | — | — | — | — | — | — | ok |
| stack_idle_rss_total_mb | ≤ 1536 MB | 210.1 | 268.8 | — | — | — | — | — | — | ok |
| ingest_lag_p99 | ≤ 2000 ms | — | — | — | — | — | — | — | — | not measured |
| dash_api_p95_overview | ≤ 200 ms | — | — | — | — | — | — | — | — | not measured |
| dash_api_p95_api_detail | ≤ 200 ms | — | — | — | — | — | — | — | — | not measured |
| web_tti | ≤ 1500 ms | — | — | — | — | — | — | — | — | not measured |
| web_perf_score | ≥ 90 score | — | — | — | — | — | — | — | — | not measured |
| web_a11y_score | ≥ 95 score | — | — | — | — | — | — | — | — | not measured |
| token_bucket_take | — | 1.718 | 1.524 | — | — | — | — | — | — | no budget |
| route_template_match | ≤ 20 us/op | — | 6.948 | — | — | — | — | — | — | ok |
| mock_direct_p50 | — | 5.393 | 5.585 | — | — | — | — | — | — | no budget |
| mock_direct_p95 | — | 10.31 | 10.06 | — | — | — | — | — | — | no budget |
| mock_direct_p99 | — | 14.38 | 13.97 | — | — | — | — | — | — | no budget |
| mock_direct_rps | — | 1591 | 1501 | — | — | — | — | — | — | no budget |
