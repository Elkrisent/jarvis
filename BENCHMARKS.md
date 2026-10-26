# Benchmarks

Numbers of record, written by `make bench-checkpoint PHASE=<n>` on the machine recorded
in each `benchmarks/checkpoints/P<n>.json` (`env`). Hosted CI runs are for trends only.
Budgets come from `benchmarks/budgets.yaml` (SPEC §13).
`—` means not measured at that checkpoint.

| Metric | Budget | P0 | P1 | P2 | P3 | P4 | P5 | P6 | P7 | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| gw_overhead_hit_p50 | ≤ 2 ms | — | — | — | — | — | — | — | — | not measured |
| gw_overhead_hit_p99 | ≤ 10 ms | — | — | — | — | — | — | — | — | not measured |
| gw_overhead_miss_p50 | ≤ 3 ms | — | 2.722 | 2.207 | — | — | — | — | — | ok |
| gw_overhead_miss_p99 | ≤ 15 ms | — | 29.33 | 7.899 | — | — | — | — | — | ok |
| gw_throughput_hit_rps | ≥ 2000 req/s | — | — | — | — | — | — | — | — | not measured |
| gw_redis_roundtrips_pre | ≤ 2 count/req | — | — | — | — | — | — | — | — | not measured |
| gw_redis_roundtrips_post | ≤ 1 count/req | — | — | — | — | — | — | — | — | not measured |
| telemetry_overhead_p99 | ≤ 0.5 ms | — | -0.486 | -0.428 | — | — | — | — | — | ok |
| telemetry_overhead_p99_redis_down | ≤ 0.5 ms | — | -0.632 | -0.386 | — | — | — | — | — | ok |
| config_propagation_p99 | ≤ 1000 ms | — | 24.93 | 33.94 | — | — | — | — | — | ok |
| redis_outage_errors | ≤ 0 count | — | — | 0 | — | — | — | — | — | ok |
| redis_outage_recovery_s | ≤ 5 s | — | — | 1.721 | — | — | — | — | — | ok |
| redis_outage_p99_ratio | ≤ 2 x | — | — | 0.972 | — | — | — | — | — | ok |
| retry_storm_calls_per_request | ≤ 1.2 count | — | — | 0.1337 | — | — | — | — | — | ok |
| s4_circuit_open_ratio | — | — | — | 0.8887 | — | — | — | — | — | no budget |
| redis_normal_p99_ms | — | — | — | 69.55 | — | — | — | — | — | no budget |
| redis_outage_p99_ms | — | — | — | 67.63 | — | — | — | — | — | no budget |
| stack_idle_rss_total_mb | ≤ 1536 MB | 210.1 | 268.8 | 248.4 | — | — | — | — | — | ok |
| ingest_lag_p99 | ≤ 2000 ms | — | — | — | — | — | — | — | — | not measured |
| dash_api_p95_overview | ≤ 200 ms | — | — | — | — | — | — | — | — | not measured |
| dash_api_p95_api_detail | ≤ 200 ms | — | — | — | — | — | — | — | — | not measured |
| web_tti | ≤ 1500 ms | — | — | — | — | — | — | — | — | not measured |
| web_perf_score | ≥ 90 score | — | — | — | — | — | — | — | — | not measured |
| web_a11y_score | ≥ 95 score | — | — | — | — | — | — | — | — | not measured |
| token_bucket_take | — | 1.718 | 1.524 | 1.743 | — | — | — | — | — | no budget |
| route_template_match | ≤ 20 us/op | — | 6.948 | 7.74 | — | — | — | — | — | ok |
| backoff_compute | — | — | — | 2.724 | — | — | — | — | — | no budget |
| mock_direct_p50 | — | 5.393 | 5.585 | 5.229 | — | — | — | — | — | no budget |
| mock_direct_p95 | — | 10.31 | 10.06 | 9.392 | — | — | — | — | — | no budget |
| mock_direct_p99 | — | 14.38 | 13.97 | 14.54 | — | — | — | — | — | no budget |
| mock_direct_rps | — | 1591 | 1501 | 1681 | — | — | — | — | — | no budget |
