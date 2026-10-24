# Checkpoint P1 — Proxy
2026-10-24 · Verified on `main` at `ab4e3a8` plus the checkpoint PR · Tag `v0.1.0` (pre-release, created after this PR merges)

## 1. In plain words
JARVIS now does its basic job. An application that calls a third-party REST API can point at JARVIS
instead, changing only the base URL and the key: JARVIS checks the key, swaps in the real provider
credential, forwards the call, and hands the provider's answer back unchanged. It refuses to be
pointed at internal or metadata addresses, picks up configuration changes (a new API, a revoked key)
within a second, keeps serving from a saved copy of its configuration when the database is down, and
writes one record per request to a stream for the dashboard that comes later. It does not yet retry,
cache, rate-limit or break circuits: every request is a single attempt. One performance target (the
tail latency of the pass-through, p99) is not met yet and is deferred to Phase 3 (section 6).

## 2. What was built
| Module | Summary | PR |
|--------|---------|----|
| M1.1 Database schema and migrations | SQLAlchemy models, reversible Alembic migration, advisory-locked migration at startup, `PostgresConfigSource` with immutable policy versions | #15 |
| M1.2 SecretBox | AES-256-GCM secrets bound to their row, `jarvis rotate-master-key` (all-or-nothing), rotation runbook | #16 |
| M1.3 Minimal management API | `/api/v1` projects, JARVIS keys (shown once, stored hashed) and APIs (secrets sealed), admin token, OpenAPI snapshot | #18 |
| M1.4 Gateway config loader | Immutable snapshots with atomic swap, Postgres loader, snapshot file fallback, Redis change events plus 60 s resync | #20 |
| M1.5 Gateway authentication | `X-Jarvis-Key` or credential-slot keys, project scope, allowed APIs, revocation within 1 s, request IDs | #21 |
| M1.6 Path templating | Route templates per API (explicit patterns, `{id}` fallback, capped) | #23 |
| M1.7 SSRF guard | Blocked ranges, numeric host forms, allowlist, DNS pinned to the checked address, save-time and connect-time checks | #24 |
| M1.8 Forwarding | Credential injection (bearer, header, query), header hygiene, body limit, streaming rejected, upstream answer passed through | #25 |
| M1.9 Telemetry emitter | One schema-v1 event per request on a Redis Stream, bounded non-blocking queue, drops counted | #27 |
| Checkpoint 1 groundwork | Resolver fix found by acceptance story 4, `sniffio`/`httptools`, S2 benchmark, GC freeze, story tests | #29, #30, #31 |
| Checkpoint 1 | Requirement tags, TLS test, budget deferral, numbers of record, this report | this PR |

## 3. How it works now
```text
1. client   GET /gw/{api_slug}/{path}   with X-Jarvis-Key (or the key in the provider's auth slot)
2. gateway  ingress: request ID, body-size and streaming checks            (before any key lookup)
3. gateway  authenticate: SHA-256 of the key -> in-memory snapshot, project, allowed APIs, revoked?
4. gateway  build the upstream request: strip the JARVIS key and any client value in the credential
            slot, inject the real credential, resolve the path under the API's base URL
5. gateway  connect through the pinned transport: resolve, check every address against the SSRF
            rules, connect to the checked address (TLS verified); one attempt, deadline from ingress
6. upstream answer streamed back byte for byte + X-Jarvis-Request-Id / Source / Attempts
7. gateway  one telemetry event queued (never waits); a background task sends batches to Redis
```
Configuration lives in Postgres and is held in memory by the gateway: a change reaches it through a
Redis event (about 25 to 40 ms measured), a full resync runs every 60 s, and the last snapshot is
saved to disk so a restart with Postgres down still serves (a snapshot older than 24 h is ignored).
With Redis down the gateway keeps serving (fail open) and telemetry is dropped and counted; the count
is in `/readyz` under `telemetry`.

## 4. Tests
| Type | Added this phase | Total | Passing |
|------|------------------|-------|---------|
| Unit | 965 | 1,282 | 1,282 |
| Integration | 136 | 145 | 145 |
| System | 9 | 16 | 16 |
| Functional | 121 | 121 | 121 |
| E2E (acceptance stories) | 4 | 8 | 8 |
| Chaos | 2 | 3 | 3 |
| Security | 120 | 133 | 133 |
| Perf (benchmarks) | 4 | 5 | 5 (run by `make bench-*`) |

- Coverage (unit + integration) against the gates: `packages/core` 100.0% line / 99.5% branch (gate 95/90); `services/gateway` 100.0% / 100.0% (90/85); `services/api` 100.0% / 100.0% (85/75); `services/worker` 95.7% / 100.0% (85/75).
- Requirements covered: **93 of 246** ([traceability](../traceability.md)). `make traceability COMPLETED_PHASE=1 TRACE_MARKERS="not perf"` passes: all 76 requirements of Phases 0 and 1 have a test and every last result is `pass`. The gate first failed on eight Phase 1 requirements whose tests existed but were not tagged (REQ-1.1-2, 2-11, 2-12, 2-13, 3-1, 17-1, 17-3) plus one with no test (REQ-11.3-3, upstream TLS verification): tagged, and a test added.
- Mutation testing: not run (scheduled for Checkpoint 3). Targeted mutation checks were run per module (each removed safeguard makes a named test fail; recorded in `docs/PROGRESS.md`).
- Skipped or xfail tests: none.
- CI on `main`: `ci`, `security`, `system` and `bench` (including the `propagation` and `telemetry` jobs) green at the last merge before this PR.

## 5. Acceptance stories
| Story | Result | Evidence |
|-------|--------|----------|
| A client changes only its base URL and key: requests succeed, the upstream receives the real credential and never the JARVIS key | Pass | `tests/e2e/test_checkpoint_1.py::test_story_1_…` (a `requests`-based SDK stand-in, upstream call log inspected); also `tests/system/test_change_one_base_url.py` and the functional suite |
| An API with base URL `http://169.254.169.254` is rejected on save | Pass | `test_story_2_a_metadata_service_base_url_is_rejected_on_save` (422 on `body.base_url`, nothing stored) |
| A key revoked via the API is rejected when the next request arrives 1 s later | Pass | `test_story_3_a_revoked_key_is_rejected_one_second_later` (401 `JV_AUTH_INVALID`) |
| With Postgres down and a snapshot present, a restarted gateway serves traffic | Pass | `test_story_4_with_postgres_down_a_restarted_gateway_serves_from_its_snapshot` (source `file`, then 200s through the gateway). It first failed and exposed a real bug (section 7). It passed in 10 of 11 stack runs afterwards; the single failure, in the first full-suite run, was not reproduced and not explained |

## 6. Benchmarks (numbers of record)
Machine (from `env.json`): Intel Core i5-1235U, 12 threads, 7.8 GB RAM, WSL2 (Linux 5.15) on Windows, Docker 28.5.1, local runner, default profile. Commit `4d110eb`.

| Metric | Budget | This phase | Previous | Status |
|--------|--------|-----------|----------|--------|
| `gw_overhead_miss_p50` (S2, 100 req/s, 50 ms upstream) | ≤ 3 ms | 2.72 ms | none | ok |
| `gw_overhead_miss_p99` (S2) | ≤ 15 ms | **29.3 ms** | none | **deferred to P3** |
| `config_propagation_p99` | ≤ 1000 ms | 24.9 ms | none | ok |
| `telemetry_overhead_p99` / `_redis_down` | ≤ 0.5 ms | -0.49 / -0.63 ms (no measurable overhead) | none | ok |
| `route_template_match` | ≤ 20 µs | 6.95 µs | none | ok |
| `stack_idle_rss_total_mb` | ≤ 1536 MB | 268.8 MB | 210.1 | ok |
| `token_bucket_take`, `mock_direct_*` | none | 1.52 µs, p50/p95/p99 5.6 / 10.1 / 14.0 ms, 1,501 req/s | 1.72 µs, 5.4 / 10.3 / 14.4 ms, 1,591 req/s | tracked |

**The S2 p99 miss.** S2 overhead is the same k6 scenario run against the mock directly and through the gateway, at a fixed 100 requests per second, median of five pairs. p50 passes. p99 does not reliably: the five pairs gave 13.9, 11.1, 55.4, 29.3 and 1,302 ms (p50 values 2.27 to 4.02 ms, worsening through the run, which may be thermal throttling on this laptop; not verified). Before the fixes below p50 was 3.1 ms and p99 76 ms. What was fixed: `sniffio` missing (about 0.9 ms of CPU per request), `httptools`, startup objects frozen out of the garbage collector (20 to 70 ms pauses every 30 s). What was ruled out by experiment: a leak, host or network jitter (a TCP relay control adds 0 ±2 ms), single-core queueing (two workers: unchanged), telemetry, connection churn. The gateway's own telemetry puts the p99 at about 12 ms in the upstream call phase and about 9 ms of gateway-side time. The gateway uses about 4 to 5 ms of CPU per request in the container and one core saturates near 140 to 190 req/s; the SPEC §13 throughput target (2,000 req/s, cache hits) is Phase 3 work. **Decision (user, at this checkpoint):** the p99 budget is enforced from Checkpoint 3, where profiling with py-spy is planned; it is measured and reported as "deferred" until then (`enforce_from_phase` in `benchmarks/budgets.yaml`, `docs/IMPLEMENTATION.md` updated). The p50 budget stays enforced.

Trends: https://elkrisent.github.io/jarvis/ (`bench/propagation`, `bench/telemetry`) · raw data: `benchmarks/checkpoints/P1.json` (includes the five S2 pairs) · table: `BENCHMARKS.md`.

## 7. Decisions and deviations
No locked decision (SPEC §2) was changed, so no ADR was needed. Decisions the user took or signed off:
- **S2 p99 budget deferred to Checkpoint 3** (section 6).
- **SSRF resolver rule relaxed narrowly (M1.7):** a JARVIS container name that has never resolved (a stopped Postgres) is treated as not running; a name that resolved before and now fails still fails closed. Lookups are shared and reused for 5 s instead of repeated for every connection. Found by acceptance story 4: with Postgres stopped, every new upstream connection waited on a failing lookup (about 2 s) and allowlisted private upstreams got 504s.
- **Empty `allowed_api_ids` means every API in the project** (M1.5); `JV_NOT_READY` (503) and the interim 501 `JV_NOT_IMPLEMENTED` accepted (M1.5).
- **Snapshot file age cap** `JARVIS_SNAPSHOT_MAX_AGE_S`, default 24 h (M1.5).
- **Telemetry:** events also for requests rejected before authentication (null ids); one `event` JSON field per stream entry plus `kind`; drop-newest when the queue is full; telemetry settings stay environment variables (M1.9).
Implementation choices: HTTP statuses for `JV_*` codes (401, 429, 429, 503, 504, 502, 413, 400, 403); `hmac.compare_digest` dropped from key lookup because the lookup is by SHA-256 digest the caller cannot steer; the gateway holds the master key to open provider secrets into memory (SPEC §11.2); dev compose publishes the gateway on 8090 (WSL reserves 8080). New pinned dependencies: sqlalchemy, alembic, asyncpg, cryptography, redis, httpx, httpcore, sniffio, httptools; dev: requests.

## 8. Known issues and tech debt
| Item | Severity |
|------|----------|
| S2 p99 overhead above budget, with occasional unexplained stalls (100 ms to seconds) in some runs; deferred to P3 | Medium |
| Revoking a key within 1 s depends on Redis pub/sub: with Redis down a revoked key keeps working until the next resync (60 s). User decision pending | Medium |
| A P5 rollback to an old API version would point at a secret that a later PATCH deleted. User decision needed before P5 | Medium (future) |
| If Postgres dies while the gateway is running, allowlisted private upstreams are refused from the next container lookup (at most 30 s later) until it returns; public upstreams are unaffected | Low |
| Acceptance story 4 failed once in 11 stack runs, not reproduced | Low, watch |
| The deadline bounds time-to-headers only: a trickling upstream body holds a slot (M2.1 owns total-time accounting) | Low |
| Smaller review notes from M1.7 and M1.8 (IDN host forms, query-credential stripping splits on `&` only, client `X-Forwarded-*`/`Via` pass through, httpx logger pin is import-time) are in `docs/PROGRESS.md` | Low |
| Telemetry has no consumer yet (the worker ingests it in P4); the stream is trimmed to about 1 M entries | Info |
| `system` workflow is not a required check (runs on `main` and labelled PRs) | Low, by design |
| 153 requirements have no test yet; they belong to Phases 2 to 7 | Info |

## 9. Try it yourself
```bash
git clone https://github.com/Elkrisent/jarvis && cd jarvis
# follow docs/quickstart-dev.md: generate JARVIS_MASTER_KEY and JARVIS_ADMIN_TOKEN, start the dev stack,
# create a project, an API (base_url of any public HTTPS API) and a key, then:
curl -i http://127.0.0.1:8090/gw/<api-slug>/<path> -H "Authorization: Bearer <your jv_live_ key>"
# the response carries X-Jarvis-Request-Id, X-Jarvis-Source: upstream, X-Jarvis-Attempts: 1
docker compose exec redis redis-cli XREVRANGE telemetry + - COUNT 1     # the event for that call
make test-all test-chaos test-security                                   # needs Docker, Postgres, Redis
DATABASE_URL=… REDIS_URL=… make bench-checkpoint PHASE=1                 # regenerates the numbers of record
```

## 10. Next phase
Phase 2, Reliability: deadlines and attempt timeouts (M2.1), the retry policy (M2.2), then retry budgets, circuit breaking and fallbacks. Nothing starts until you approve this checkpoint.
Questions for you: (1) how long a revoked key may keep working if Redis is down (today up to 60 s; the options are a shorter resync, a Postgres check on a miss, or accepting it); (2) the P5 secret-rollback question; (3) whether to add the S2 benchmark to the hosted `bench.yml` (it needs the full stack, so it runs locally for now).
