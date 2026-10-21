# Checkpoint P0 — Foundations
2026-10-21 · Verified on `main` at `8f8d301` plus the checkpoint PR · Tag `v0.0.0` (pre-release, created after this PR merges)

## 1. In plain words
JARVIS does not proxy any API calls yet. What exists is the ground it will stand on: a repository
where every change is checked automatically, a stack of containers (gateway, api, worker, Postgres,
Redis) that starts healthy with one command and reports honestly when Redis goes away, a shared
code library with the configuration model and the building blocks the gateway will use, a fake
upstream API that tests can make slow, flaky or broken on demand, and a benchmark setup that
publishes numbers to a chart. Nothing a user would notice yet, but every later phase is tested and
measured from day one.

## 2. What was built
| Module | Summary | PR |
|--------|---------|----|
| M0.1 Repository scaffold and tooling | uv workspace, ruff, mypy strict (core, gateway), pytest markers, a rule that fails any test without a type marker, pre-commit, Makefile, templates, ADRs 0001–0013 | #1 |
| M0.2 Core package | `ApiDependency` models with cross-field validation, `JarvisError` hierarchy with `JV_*` codes, request and auth objects, `Clock`/`Random`, eight interfaces with in-memory implementations and reusable contract suites, 246 requirement IDs, traceability script | #3 |
| M0.3 Compose skeleton and health | `/healthz` and `/readyz` for gateway, api and worker, non-root multi-stage images, compose default/dev/test files | #5 |
| M0.4 CI workflows | `ci`, `security` and `system` workflows, per-package coverage gates, bandit, pip-audit, gitleaks, trivy, caching, branch ruleset | #7 |
| M0.5 Mock upstream | Rule-driven fake API with admin API, call log, seeded mode, packaged in the compose test profile | #10 |
| M0.6 Benchmark infrastructure | Budgets from SPEC §13, micro-benchmark, k6 dry run, converters and checkers, `bench.yml`, gh-pages charts | #11 |
| Checkpoint 0 | Acceptance-story tests, repo-config tests, traceability fixes (this PR) | this PR |

## 3. How it works now
There is no request path yet. The running stack is:

```text
1. gateway  GET /healthz, GET /readyz      (Redis optional: fail open)
2. api      GET /healthz, GET /readyz      (Postgres required, Redis optional)
3. worker   GET /healthz, GET /readyz      (Postgres and Redis required)
4. redis, postgres (TimescaleDB)           (health-checked; services wait for them)
5. mock-upstream (test profile only)       (rules set over /__admin/*)
```

`/readyz` answers `ok` (200), `degraded` (200, an optional dependency is down) or `unavailable`
(503, a required one is down). The chaos test shows it live: stop Redis and the gateway and api
report `degraded`, the worker reports `unavailable`; start Redis and all return to `ok`.

## 4. Tests
| Type | Added this phase | Total | Passing |
|------|------------------|-------|---------|
| Unit | 317 | 317 | 317 |
| Integration | 9 | 9 | 9 |
| System | 7 | 7 | 7 |
| Functional | 0 (see known issues) | 0 | n/a |
| E2E (acceptance stories) | 4 | 4 | 4 |
| Chaos | 1 | 1 | 1 |
| Security | 13 | 13 | 13 |
| Perf (micro-benchmark) | 1 | 1 | 1 |

- Coverage (unit suite) against the gates: `packages/core` 100.0% line / 98.9% branch (gate 95/90); `services/gateway`, `api`, `worker` 95.7% / 100% (gates 90/85, 85/75).
- Requirements covered: **30 of 246** ([traceability](../traceability.md)). `make traceability COMPLETED_PHASE=0 TRACE_MARKERS="not perf"` passes: every Phase 0 requirement has a test and all 30 last results are `pass`. The other 216 belong to later phases.
- Mutation testing: not run (scheduled for Checkpoint 3).
- Skipped or xfail tests: none.
- Security scans on `main`: bandit, pip-audit, gitleaks and trivy (three images) all green.

## 5. Acceptance stories
| Story | Result | Evidence |
|-------|--------|----------|
| Given a clean machine, when I clone and run `docker compose up -d`, all services are healthy | Pass | `tests/e2e/test_checkpoint_0.py::test_story_1_fresh_clone_comes_up_all_healthy` clones the repo, copies `.env.example`, and checks `postgres`, `redis`, `gateway`, `api`, `worker` are all `healthy` |
| Given a PR with a failing test, when CI runs, the merge is blocked | Pass | PR #8 (closed unmerged) had a failing `python` check and GitHub reported `BLOCKED`, not mergeable even as admin. Ongoing guard: `test_story_2_main_is_protected_so_a_failing_pr_cannot_merge` checks the active ruleset (PR required, nine required checks that all exist, strict, no bypass, no force push) |
| Given a push to main, when `bench.yml` runs, a data point appears on the gh-pages chart | Pass | `bench.yml` run 39575593691 on `main`; `test_story_3_bench_chart_has_a_data_point_on_gh_pages` reads https://elkrisent.github.io/jarvis/bench/load/data.js and finds `mock_direct_p50` and `mock_direct_p99` |

## 6. Benchmarks (numbers of record)
Machine (from `env.json`): Intel Core i5-1235U, 12 threads, 7.8 GB RAM, WSL2 (Linux 5.15) on Windows, Docker Desktop, local runner, default profile.

| Metric | Budget | This phase | Previous | Status |
|--------|--------|-----------|----------|--------|
| `stack_idle_rss_total_mb` | ≤ 1536 MB | 210.1 | none | ok |
| `token_bucket_take` | none | 1.72 µs/op | none | tracked |
| `mock_direct_p50` / `p95` / `p99` | none | 5.39 / 10.3 / 14.4 ms | none | tracked |
| `mock_direct_rps` | none | 1,591 req/s | none | tracked |

The gateway budgets of SPEC §13 (overhead, throughput, Redis round trips, propagation) are not in scope for this phase: there is no gateway pipeline to measure. The `mock_direct_*` values test the pipeline itself.
Trends: https://elkrisent.github.io/jarvis/ · raw data: `benchmarks/checkpoints/P0.json` · table: `BENCHMARKS.md`.

## 7. Decisions and deviations
No locked decision (SPEC §2) was changed, so no ADR was needed. Implementation-level choices:
- **HTTP statuses for the `JV_*` codes** are not in the spec; chosen: 401, 429, 429, 503, 504, 502, 413, 400, 403.
- **`/readyz` semantics** (ok / degraded / unavailable) are my definition of "dependencies reachable"; checks are TCP reachability for now.
- **`REQ-15.1-1`** (Caddy and the monitoring profile in compose) moved from P0 to P4, because Caddy and the web app arrive with the dashboard.
- **Test profile** is an override file (`docker-compose.test.yml`) because compose profiles cannot publish ports on an existing service; the mock upstream is in the `test` profile inside it.
- **`budgets.yaml`** is hand-written from SPEC §13, with a test that checks the values.
- **`bench.yml`** has `micro` and `load` jobs only; soak, web and AI-eval jobs come with their suites.
- **Dependency upgrades** forced by the security gates: `fastapi 0.156.5` (`starlette 1.9.0`), `pytest 9.1.1`, `pytest-asyncio 1.4.0`, `python 3.12.15` base image.
- **Mock upstream `reset`** drops the connection after sending headers and one body byte; a true TCP RST cannot be produced from an ASGI app.

## 8. Known issues and tech debt
| Item | Severity |
|------|----------|
| No functional test suite yet: `make test-functional` is a placeholder until the gateway exists (REQ-linked tests so far are unit, system and e2e) | Low |
| One unexplained local failure of `compose up --wait` after the mock upstream was added; not reproduced in later runs or in CI | Low, watch |
| A `hang` request in the mock keeps its handler until server shutdown | Low (test asset) |
| `system` workflow is not a required check (it only runs on `main` and labelled PRs) | Low, by design |
| Trivy scans our three images, not the pinned Postgres and Redis images | Low |
| Hosted-runner benchmarks are trend-only; numbers of record are local | Info |
| 216 requirements have no test yet; they belong to Phases 1–7 | Info |

## 9. Try it yourself
```bash
git clone https://github.com/Elkrisent/jarvis && cd jarvis/deploy
cp .env.example .env            # set POSTGRES_PASSWORD
docker compose up -d --build --wait
docker compose exec gateway python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/readyz').read())"
docker compose stop redis       # then read /readyz again: degraded
docker compose start redis
cd .. && make test-all test-chaos test-security     # needs Docker and uv
make bench-checkpoint PHASE=0                       # regenerates the numbers of record
```

## 10. Next phase
Phase 1, Proxy: routing, config from memory and Postgres, JARVIS keys, credential injection, the SSRF guard, request IDs and telemetry to the stream. First module is M1.1 (config repositories and Alembic). Nothing starts until you approve this checkpoint.
Questions for you: none blocking. Optional: whether you want `system` made a required check once the gateway exists.
