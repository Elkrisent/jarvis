# Checkpoint P2 — Reliability
2026-10-26 · Verified on `main` at `0b7cd7b` plus the checkpoint PR · Tag `v0.2.0` (pre-release, approved 2026-10-26)

## 1. In plain words
JARVIS now protects an application from a misbehaving provider. Every request has a deadline and
each attempt has its own timeout. Failures that are safe to repeat are retried with backoff, but
only within a budget (20% of recent traffic), so a struggling provider is not hammered. When a
provider keeps failing, a circuit breaker stops sending it traffic and answers at once, then lets
one probe through to test recovery. Rate limits (per key) and an upstream quota protect both sides,
and an API can name a fallback API that serves when the primary fails. If Redis is lost, the gateway
keeps serving from local state and says so in `/readyz`. Caching and the dashboard are not built yet
(Phases 3 and 4).

## 2. What was built
| Module | Summary | PR |
|--------|---------|----|
| M2.1 Deadlines | Request deadline from ingress, per-attempt timeout, body reads and responses bounded by the deadline | #36 |
| M2.2 Retry policy | Retryable conditions, method and idempotency rules, `Retry-After`, full-jitter backoff | #37 |
| M2.3 Retry budget | Rolling 60 s budget per API shared through Redis, local fallback, exhaustion events | #38 |
| M2.4 Circuit breaker | Shared state machine, one half-open probe through a Redis lock, doubling open time, transition events | #39 |
| M2.5 Rate limiters | Atomic token buckets: inbound per (project, key, API) and upstream quota per API, off by default | #40 |
| M2.6 Fallback | Fallback API with its own circuit, quota, retries and credential; `X-Jarvis-Source: fallback`; save-time validation | #41 |
| M2.7 Redis fail-open | Redis health breaker, local state while down, `/readyz` degraded, `redis_unavailable` event, `JARVIS_REDIS_FAIL_OPEN` | #42 |
| Groundwork | Revocation poll every 5 s when pub/sub is down (#34); stack tests tolerate event records, S4 and S8 k6 scenarios (#43) | #34, #43 |
| Checkpoint 2 | Acceptance stories, requirement tags, numbers of record, this report | this PR |

## 3. How it works now
```text
1. client   GET /gw/{api}/{path}
2. gateway  ingress, authenticate, inbound rate limit (429 JV_RATE_LIMITED + Retry-After)
3. gateway  circuit check: OPEN -> 503 JV_CIRCUIT_OPEN at once, no upstream call, no body read
4. gateway  upstream quota token (429 JV_QUOTA_EXCEEDED), then the attempt (timeout = min(attempt, time left))
5. failure  retryable and allowed by method/idempotency/Retry-After/deadline/budget/quota -> backoff, goto 4
6. still failing and the API names a fallback -> steps 3 to 5 against the fallback API (same deadline)
7. response + X-Jarvis-Attempts / X-Jarvis-Circuit / X-Jarvis-Source; telemetry request record plus
   separate event records (circuit_transition, retry_budget_exhausted, fallback, redis_unavailable)
```
Circuit, retry budget and limiter counts live in Redis so several gateways share them. When Redis
fails (three errors in a row or one timeout), those three features switch to local state at once
(limits divided by `JARVIS_EXPECTED_INSTANCES`) and a background PING brings them back.

## 4. Tests
| Type | Added this phase | Total | Passing |
|------|------------------|-------|---------|
| Unit | 522 | 1,804 | 1,804 |
| Integration | 33 | 178 | 178 |
| System | 0 | 16 | 16 |
| Functional | 52 | 173 | 173 |
| E2E (acceptance stories) | 4 | 12 | 12 |
| Chaos | 0 | 3 | 3 |
| Security | 0 | 133 | 133 |
| Perf (benchmarks) | 2 | 7 | 7 (run by `make bench-*`) |

- All 2,319 non-perf tests passed together in the final `make traceability` run (Docker stack, throwaway TimescaleDB/Postgres 16, Redis 7). `make coverage` (1,982 tests) passed: `packages/core` 100.0% line / 99.6% branch, `services/gateway` 100.0 / 100.0, `services/api` 100.0 / 100.0, `services/worker` 95.7 / 100.0, all above their gates.
- Requirements covered: **129 of 246** ([traceability](../traceability.md)). `make traceability COMPLETED_PHASE=2 TRACE_MARKERS="not perf"` passes. The gate first failed on seven Phase 2 requirements: six were built and tested but untagged (now tagged: REQ-4.3-4, -4.3-7, -4.4-3, -6.1-3, -8-7, plus REQ-3-3 which story 4 now checks by confirming projects, APIs and keys are intact after Redis is killed); REQ-4.12-1 (`serve_stale`) needs the cache and was retagged P3 by your decision.
- Mutation testing: not run (scheduled for Checkpoint 3). Per-module mutation checks are recorded in `docs/PROGRESS.md`.
- Skipped or xfail tests: none.
- CI on `main`: the `system` workflow failed after #42 (the M2.7 event record broke a stack test); fixed in #43. Checks on #43 were green; `main` after #43 has not been re-checked by me.

## 5. Acceptance stories
| Story | Result | Evidence |
|-------|--------|----------|
| Upstream failing 60% of the time: the circuit opens, requests fail fast, one probe tests recovery, it closes when healthy | Pass | `tests/e2e/test_checkpoint_2.py::test_story_1_…`: 20 refused requests make zero upstream calls (`X-Jarvis-Circuit: OPEN`); 30 concurrent requests after the open period make exactly one upstream call; after recovery the next probe closes it |
| POST without an idempotency config on a 503 is not retried | Pass | `test_story_2_…`: one upstream call, `X-Jarvis-Attempts: 1`; a GET to the same upstream is retried (control) |
| Quota of 60/min, 200 requests: the upstream receives at most 60 | Pass | `test_story_3_…`: upstream received 60 plus at most what the run's duration refills; the rest were 429 `JV_QUOTA_EXCEEDED` |
| Redis killed during load: no request fails because of JARVIS | Pass | `test_story_4_…`: four clients, Redis stopped, `/readyz` degraded, Redis restarted, every response 200; no durable data lost |

## 6. Benchmarks (numbers of record)
Machine (from `env.json`): Intel Core i5-1235U, 12 threads, 7.8 GB RAM, WSL2 on Windows, Docker 28.5.1, local runner, default profile. `make bench-checkpoint PHASE=2` ran to the end with every enforced budget met.

| Metric | Budget | This phase | Previous | Status |
|--------|--------|-----------|----------|--------|
| `gw_overhead_miss_p50` (S2) | ≤ 3 ms | 2.21 ms | 2.72 | ok |
| `gw_overhead_miss_p99` (S2) | ≤ 15 ms, enforced from P3 | 7.90 ms | 29.3 | ok (see below) |
| `retry_storm_calls_per_request` (S4, 50% 503) | ≤ 1.2 | 0.134 | none | ok |
| `s4_circuit_open_ratio` | tracked | 0.889 | none | tracked |
| `redis_outage_errors` (S8) | 0 | 0 of 4,501 requests | none | ok |
| `redis_outage_p99_ratio` (S8) | ≤ 2 | 0.97 (69.6 → 67.6 ms) | none | ok |
| `redis_outage_recovery_s` (S8) | ≤ 5 s | 1.72 s | none | ok |
| `config_propagation_p99` | ≤ 1000 ms | 33.9 ms | 24.9 | ok |
| `telemetry_overhead_p99` / `_redis_down` | ≤ 0.5 ms | -0.43 / -0.39 ms | -0.49 / -0.63 | ok |
| `route_template_match` | ≤ 20 µs | 7.74 µs | 6.95 | ok |
| `stack_idle_rss_total_mb` | ≤ 1536 MB | 248.4 MB | 268.8 | ok |
| `token_bucket_take`, `backoff_compute` | none | 1.74 µs, 2.72 µs | 1.52 µs, none | tracked |

**S2 p99 and the py-spy profile.** The p99 passed this run (7.9 ms) but it is not stable: a second S2 run during profiling gave 16.5 ms, and Checkpoint 1 gave 29.3 ms, with the same code path. So the 15 ms budget is still not reliably met on this machine, and you kept enforcement at Checkpoint 3. The py-spy profile (`benchmarks/profiles/P2/gateway.raw`, 25 s at 250 Hz, taken inside the gateway container's process namespace while S2 ran at 100 req/s) shows the gateway busy for only about 4% of samples (262 of about 6,250): the rest is idle waiting. Everything that ran is background work (circuit refresh, snapshot writes, config resync), and no single function on the request path stands out. I did not find a CPU hotspot to fix; the tail looks like scheduling noise on a shared laptop, which matches the Checkpoint 1 stall investigation. This profile has too few samples on the request path to settle it. Phase 3 should profile with a longer window or higher load (S1) before that budget is enforced.

Raw data: `benchmarks/checkpoints/P2.json` · table: `BENCHMARKS.md`.

## 7. Decisions and deviations
No locked decision (SPEC §2) was changed, so no ADR was needed. Decisions you took this phase:
- **S2 p99 stays deferred to Checkpoint 3.** `docs/IMPLEMENTATION.md` said it was enforced here; it was corrected to match `budgets.yaml` and your Checkpoint 1 decision.
- **REQ-4.12-1 (`serve_stale`) retagged P3** (`docs/requirements.md`), since it needs the cache.
- Revocation: while the config pub/sub link is down the gateway polls Postgres every 5 s instead of 60 s (decided earlier, shipped in #34).

Interpretations where the SPEC is silent, **still waiting for your confirmation** (also in `docs/PROGRESS.md`):
1. `JARVIS_REDIS_FAIL_OPEN=false` answers 503 `JV_NOT_READY` while Redis is down (M2.7).
2. A final upstream 5xx triggers the fallback API; the fallback is checked against the primary's inbound limit and key scope only (M2.6).
3. The retry budget is strict: no retry until about five requests are in the window at ratio 0.2. A lone request is never retried (M2.3).
4. A caller-supplied idempotency key is kept; a generated one is added only for POST/PATCH-like requests (M2.2).
5. The first retry's backoff ceiling is `base_ms` (M2.2).

## 8. Known issues and tech debt
| Item | Severity |
|------|----------|
| S2 p99 swings between about 8 and 30 ms from run to run; cause not found (profile is inconclusive); enforcement starts at P3 | Medium |
| Timing tests flake on a loaded machine: `test_deadlines.py` (two tests failed once during the full 2,300-test run with the Docker stack up, pass alone 3 of 3) and `test_circuit_shared.py` (failed once in two `make coverage` runs: a 0.25 s Redis timeout under load let extra probes through, which is the designed fail-open). Not loosened. The circuit test has been timing-sensitive since M2.7 tightened the Redis timeouts | Medium |
| httpx pool is 100 connections per upstream; more concurrent requests to one upstream get pool-timeout 504s. The retry budget and circuit store Redis clients use redis-py's default pool of 100 (the limiter has a blocking pool) | Medium, decide in P3 |
| In Docker the name `redis` takes about 4 s to fail DNS while the container is down; most of the S8 recovery time (1.7 s here, 2.6 s earlier) is that, not JARVIS | Low |
| Revoking a key within 1 s depends on Redis pub/sub; with Redis down it takes up to 5 s (was 60 s) | Low |
| A P5 rollback to an old API version could point at a secret that a later PATCH deleted; the rule is decided (see `docs/PROGRESS.md`) and must be built in P5 | Medium (future) |
| The `bench` workflow's `load` job failed on `main` after #39 and #40 because the mock upstream's own latency doubled (runner noise, not confirmed) | Low, watch |
| `bench-circuit` is not run by `bench.yml` (tracked metric only) | Low |
| Telemetry has two record shapes now (`request` and `event`); the Phase 4 worker must handle both | Info |
| 117 requirements have no test yet; they belong to Phases 3 to 7 | Info |

## 9. Try it yourself
```bash
git clone https://github.com/Elkrisent/jarvis && cd jarvis
make test-e2e                                   # needs Docker; includes the four Checkpoint 2 stories
make test-all test-chaos test-security          # needs Docker, Postgres, Redis
DATABASE_URL=… REDIS_URL=… make bench-checkpoint PHASE=2
# by hand, with the compose test stack up: point an API at the mock upstream with
#   retry/circuit/rate_limits settings, then watch the headers:
curl -i http://127.0.0.1:18080/gw/<api-slug>/v1/x -H "Authorization: Bearer <jv_live_ key>"
#   X-Jarvis-Attempts, X-Jarvis-Circuit and X-Jarvis-Source show what happened
```

## 10. Next phase
Phase 3, Performance: the cache (M3.1, including `serve_stale`) and the profiling-driven tuning that follows. Nothing starts until you approve this checkpoint.
Questions for you: (1) the five interpretations in section 7; (2) the httpx and redis-py pool sizes (section 8) before the performance work; (3) whether the S2 p99 budget should be enforced at Checkpoint 3 given the run-to-run spread (about 8 to 30 ms on this machine).
