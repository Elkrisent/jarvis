# JARVIS — Implementation Guide for Claude Code

Companion to `docs/SPEC.md` (JARVIS Technical Specification v0.1).
This guide defines **the order of work, the testing required at every step, how benchmarks are logged to GitHub, and what happens at each checkpoint.**

---

## Part A — Operating Rules

### A.1 Setup for Claude Code

Place the files in the repository like this:

```text
docs/SPEC.md                    ← the technical specification
docs/IMPLEMENTATION_GUIDE.md    ← this file
CLAUDE.md                       ← short pointer file (below)
```

Contents of `CLAUDE.md`:

```markdown
# JARVIS — instructions for Claude Code
- Source of truth: docs/SPEC.md. Work order and rules: docs/IMPLEMENTATION_GUIDE.md.
- Before any work, read both files. Find the current module in docs/PROGRESS.md.
- Build ONE module at a time, in guide order. Never skip its tests.
- At every checkpoint: run the checkpoint protocol (Guide §D), then STOP and wait for approval.
- Before starting frontend work (M4.7): ask the user the questions in Guide Appendix 1. Do not guess.
- Never weaken, skip, or delete a test to get green. Never change a locked decision (SPEC §2) without asking.
```

### A.2 Working Rules

1. **The spec is the source of truth.** If code and spec disagree, the code is wrong. If you believe the spec is wrong, stop and ask the user. Any agreed change is recorded as an ADR in `docs/adr/` and in the spec.
2. **Follow the module order.** Each module has a Definition of Done (DoD). Don't start the next module until the current DoD is met and CI is green.
3. **Track progress.** Keep `docs/PROGRESS.md` updated: current phase, current module, completed modules with commit SHA, and open questions.
4. **Write tests first for logic.** State machines, algorithms, parsers and policies (token bucket, backoff, circuit breaker, cache eligibility, templating, redaction) have their tests written from the spec's requirements *before* the implementation.
5. **Use real infrastructure in integration tests.** Integration tests run against real Postgres/Timescale and Redis, via the compose test profile or CI service containers. Never mock Redis or Postgres in integration tests.
6. **Keep tests deterministic.** Time and randomness are injected through the `Clock` and `Random` interfaces in `packages/core`. No `time.sleep` in unit tests. Integration tests may wait only via polling helpers with timeouts.
7. **Test integrity:**
   - Never delete, skip, or loosen an assertion to pass CI.
   - `xfail` or `skip` requires a linked GitHub issue and must be reported in the next checkpoint.
   - A flaky test is a bug: fix it or quarantine it with an issue. CI does not retry tests automatically.
8. **Bug fixes start with a failing test.** Every bug, whether found by you or reported, gets a regression test that fails before the fix and passes after it. Reference the issue in the test docstring.
9. **Git discipline:**
   - One branch per module: `feat/m<phase>.<n>-<slug>`, e.g. `feat/m2.4-circuit-breaker`.
   - Conventional commits (`feat:`, `fix:`, `test:`, `perf:`, `docs:`, `ci:`, `refactor:`, `chore:`).
   - Open a PR with `gh pr create` using the PR template. Squash-merge after CI is green.
   - Never push directly to `main`. Never commit secrets or `.env`.
10. **Dependencies.** Pin versions. Adding a dependency not named in the spec needs a one-line justification in the PR. Heavy ones (a new service, a framework, a database) require asking the user first.
11. **When to ask the user:**
    - The spec is ambiguous in a way that changes behavior
    - A change would affect a locked decision
    - You're about to start frontend work
    - A performance budget can't be met without an architectural change
    - Otherwise, decide, document the decision in the PR, and continue.
12. **Update docs as you go.** Each module updates the relevant `docs/` pages (configuration reference, architecture notes) in the same PR.

### A.3 Additions to the Spec

These are implementation-level additions and don't change behavior:

- `packages/core` gets `Clock` (`now()`, `monotonic()`, `sleep()`) and `Random` interfaces, with real implementations and fake implementations for tests.
- A **mock upstream** service (`tools/mock_upstream/`) that tests can control. It's a first-class test asset (M0.5).
- A custom pytest marker `@pytest.mark.req("REQ-x.y-n")` for requirement traceability (§B.4).

---

## Part B — Testing Framework

### B.1 Test Types and Where They Live

| Type | Purpose | Tools | Location | Runs |
|------|---------|-------|----------|------|
| **Unit** | One function/class in isolation, fakes for I/O | pytest, pytest-asyncio, hypothesis | `services/*/tests/unit/`, `packages/core/tests/unit/` | Every PR |
| **Integration** | A module against real Redis/Postgres or another module | pytest + service containers | `services/*/tests/integration/` | Every PR |
| **System** | The whole stack running in compose, black-box through public interfaces | pytest + httpx against compose | `tests/system/` | Every PR (label `system` or main) |
| **Functional** | Verifies each spec requirement (REQ-ID) behaves as specified; usually system-level, some integration | pytest with `@req` markers | `tests/system/functional/` | Every PR |
| **Acceptance / E2E** | User stories per phase (Given/When/Then), through the gateway and the UI | pytest (API flows), Playwright (UI flows) | `tests/e2e/` | main + checkpoints |
| **Regression** | Guards against reintroduced bugs and silent changes | All suites + OpenAPI contract snapshot + visual snapshots + benchmark gates | Distributed; bug tests named `test_regression_<issue>` | Every PR |
| **Non-functional: performance** | Latency, throughput, overhead vs §13 budgets | k6, pytest-benchmark | `tests/load/`, `*/tests/bench/` | Nightly + checkpoints |
| **Non-functional: resilience** | Behavior when Redis, Postgres, worker, or upstream fail | pytest + docker compose control | `tests/chaos/` | Nightly + checkpoints |
| **Non-functional: security** | SSRF, secret leakage, auth, dependency and image vulnerabilities | pytest, bandit, pip-audit, npm audit, trivy, gitleaks | `tests/security/` + `security.yml` | Every PR + weekly |
| **Non-functional: resource/soak** | Memory growth, container footprint | k6 soak + docker stats sampler | `tests/load/soak/` | Nightly (short) + checkpoints (long) |
| **Non-functional: UI** | Accessibility, frontend performance, visual consistency | axe (via Playwright), Lighthouse CI, Playwright screenshots | `web/tests/` | Every PR touching `web/` |

Frontend unit and component tests use **Vitest + Testing Library** in `web/src/**/*.test.tsx`.

### B.2 Markers and Commands

pytest markers are declared in the root `pyproject.toml`: `unit`, `integration`, `system`, `functional`, `e2e`, `chaos`, `security`, `perf`, `slow`, `req(id)`.

A `Makefile` provides these targets, and CI calls the same targets:

```text
make test-unit          make test-integration     make test-system
make test-functional    make test-e2e             make test-chaos
make test-security      make test-web             make test-all
make bench-micro        make bench-load           make bench-soak
make bench-checkpoint   make traceability         make coverage
```

### B.3 Coverage Gates

| Package | Line coverage | Branch coverage |
|---------|---------------|-----------------|
| `packages/core` | ≥ 95% | ≥ 90% |
| `services/gateway` | ≥ 90% | ≥ 85% |
| `services/api`, `services/worker` | ≥ 85% | ≥ 75% |
| `web/` | ≥ 80% statements (components + hooks) | — |

CI fails if coverage drops below these gates, or drops more than 1 point relative to `main`. Coverage reports are uploaded as artifacts and summarized in the job summary.

At Checkpoint 3, run **mutation testing** (`mutmut`) on the core algorithms: token bucket, backoff, circuit breaker, cache eligibility, templating. Target ≥ 80% of mutants killed, and report the result.

### B.4 Requirement Traceability

- In M0.2, extract every testable requirement from the spec into `docs/requirements.md`, with IDs `REQ-<spec-section>-<n>`. Examples: `REQ-4.7-3` = "POST is retried only when an idempotency header is configured"; `REQ-4.9-2` = "responses with `Set-Cookie` are never cached".
- Every functional test carries `@pytest.mark.req("REQ-…")`. One test may cover several IDs.
- `make traceability` generates `docs/traceability.md`: each requirement → tests → last result. **Uncovered requirements for completed phases fail CI.**

### B.5 Regression Protection

1. The full unit, integration, system and functional suite runs on every PR.
2. Every bug fix follows rule A.2.8.
3. **API contract snapshot:** `tests/contract/openapi.snapshot.json`. CI fails if the generated OpenAPI changes without the snapshot being updated in the same PR. The PR description must explain the change.
4. **Telemetry schema snapshot:** changes to the event schema (SPEC §6.1) require bumping `v` and updating the snapshot.
5. **Visual regression:** Playwright screenshots of key pages (light and dark) are compared against baselines. Intentional changes update the baselines in the same PR.
6. **Benchmark regression gates** (§C.4).
7. **Migration regression:** from P4 onward, a test upgrades the database from the previous checkpoint's schema with seeded data and verifies the data survives.

### B.6 Standard Test Fixtures

- `compose_stack`: brings up the compose `test` profile once per session, with a fresh database per test module (via template databases) and `FLUSHALL` between tests.
- `mock_upstream`: a client for the mock upstream admin API (M0.5).
- `fake_clock`, `fake_random`: deterministic time and randomness.
- `gateway_client`, `api_client` (admin token), `seeded_project`: a project with a key and one API pointed at the mock upstream.
- `kill(service)` / `revive(service)`: for chaos tests, wrapping `docker compose stop/start`.

---

## Part C — Benchmarks and GitHub Logging

### C.1 What Gets Measured

| Suite | Metric (name used in logs) | Unit | Direction |
|-------|----------------------------|------|-----------|
| Micro (pytest-benchmark) | `token_bucket_take`, `cache_key_build`, `route_template_match`, `backoff_compute`, `redaction_apply`, `evidence_bundle_build` | µs/op | lower |
| Gateway load (k6) | `gw_overhead_hit_p50`, `gw_overhead_hit_p99`, `gw_overhead_miss_p50`, `gw_overhead_miss_p99` | ms | lower |
| | `gw_throughput_hit_rps` (at p99 ≤ budget) | req/s | higher |
| | `gw_redis_roundtrips_pre`, `gw_redis_roundtrips_post` | count/req | lower |
| | `dedup_upstream_calls_per_1000_identical` | count | lower |
| | `upstream_calls_avoided_ratio` (mixed workload) | % | higher |
| | `config_propagation_p99` | ms | lower |
| | `circuit_open_fail_fast_p99` | ms | lower |
| Soak | `gw_rss_slope_mb_per_hour`, `worker_rss_slope_mb_per_hour` | MB/h | lower |
| Footprint | `stack_idle_rss_total_mb` | MB | lower |
| Telemetry | `ingest_lag_p99`, `telemetry_dropped_ratio` | ms / % | lower |
| Dashboard API | `dash_api_p95_overview`, `dash_api_p95_api_detail` | ms | lower |
| Web (Lighthouse CI) | `web_lcp`, `web_tti`, `web_bundle_js_kb` | ms / KB | lower |
| | `web_perf_score`, `web_a11y_score` | 0–100 | higher |
| AI eval | `ai_eval_score_<provider>`, `ai_ttft_p50_<provider>`, `ai_tokens_per_explanation_<provider>` | % / ms / tokens | higher / lower / lower |

**How gateway overhead is measured:** the mock upstream is set to a fixed latency (e.g. 50 ms). Run the same k6 scenario against the mock directly and through JARVIS. Overhead = JARVIS latency − direct latency, computed per percentile. The gateway also reports `upstream_latency_ms` in telemetry, which serves as a cross-check.

### C.2 Load Scenarios (`tests/load/scenarios/`)

| ID | Scenario | Purpose |
|----|----------|---------|
| S1 | Cache-hit heavy (95% repeat keys) | Hit overhead + throughput |
| S2 | Pass-through (unique keys, cache off) | Miss overhead |
| S3 | Duplicate burst: 1,000 identical concurrent requests | Dedup effectiveness |
| S4 | Failing upstream (50% 503) | Retry budget + circuit fail-fast |
| S5 | Mixed realistic (weights configurable) | Upstream calls avoided |
| S6 | Stress ramp until p99 > budget | Breaking point, recorded as a number |
| S7 | Soak (nightly 20 min; checkpoint 2 h) | Memory growth |
| S8 | S5 while Redis is killed and restored | Fail-open under load |

### C.3 Logging to GitHub

Benchmark results are recorded in five places, each for a different purpose:

```text
1. Job summary (every run)
   markdown table: metric · value · budget · Δ vs main
   written to $GITHUB_STEP_SUMMARY
   visible on the Actions run page

2. Artifacts (every run)
   raw k6 JSON, pytest-benchmark JSON, env.json
   Lighthouse reports, docker stats samples
   retained 90 days

3. Trend dashboard (main + nightly)
   benchmark-action/github-action-benchmark
   auto-pushes to gh-pages → /bench/{micro,load,web,ai}
   charts per metric over commits

4. PR comments (on regression)
   comment-on-alert at 120% of baseline
   fail-on-alert at 150% (noisy hosted runners)
   absolute §13 budgets checked separately

5. Checkpoint snapshots (committed)
   benchmarks/checkpoints/P<n>.json (raw + env)
   BENCHMARKS.md table updated per checkpoint
   official numbers of record
```

**Details:**
- Every result file includes `env.json`: commit SHA, branch, date, runner type (`github-hosted`/`local`), CPU model, core count, RAM, OS, Python/Node versions, JARVIS config profile, and the scenario parameters.
- `scripts/bench/k6_to_gab.py` converts k6 summaries into the `customSmallerIsBetter` / `customBiggerIsBetter` formats. pytest-benchmark output uses the action's native `pytest` tool.
- `scripts/bench/check_budgets.py` compares results with the §13 budgets from `benchmarks/budgets.yaml` (one file, generated from the spec) and fails the job on a violation. On GitHub-hosted runners, absolute budgets are **reported but not enforced**. They are enforced in `make bench-checkpoint`.
- **Numbers of record** come from `make bench-checkpoint`, which runs locally on a documented machine (recorded in `env.json`), runs S1–S8, and writes `benchmarks/checkpoints/P<n>.json`. It also regenerates the `BENCHMARKS.md` table with columns: metric · budget · P1 · P2 · … · status. Hosted runners are too noisy for absolute latency claims, so they're used only for trends and relative regressions.
- Workflow file: `.github/workflows/bench.yml`, triggered nightly, manually, and on push to `main`, with jobs `micro`, `load`, `soak-short`, `web`, `ai-eval` (secrets-gated). Enable GitHub Pages from the `gh-pages` branch in M0.6.

### C.4 Regression Gates

| Suite | On PR | On main / nightly |
|-------|-------|-------------------|
| Micro | Comment at +20%, fail at +50% vs `main` baseline | Push to trend dashboard |
| Load (hosted) | Run S1, S2, S3 in short mode; comment at +20% | Full S1–S6 + S8, push to trend dashboard |
| Web | Lighthouse CI assertions (perf ≥ 90, a11y ≥ 95, bundle ≤ budget) fail the PR | Push trend |
| Checkpoint | — | All §13 budgets enforced; failure blocks the checkpoint |

---

## Part D — Checkpoint Protocol

A checkpoint happens at the end of each phase. Claude Code performs these steps **in order**:

1. **Verify every module DoD** in the phase and tick them off in `docs/PROGRESS.md`.
2. **Run the full suite:** `make test-all`, plus `make test-chaos` and `make test-security`. CI must also be green on `main`.
3. **Run the phase acceptance stories** (listed in each phase) with `make test-e2e`, then record the results.
4. **Run `make bench-checkpoint`.** Commit `benchmarks/checkpoints/P<n>.json` and the updated `BENCHMARKS.md`. Any budget in scope for this phase that is missed blocks the checkpoint: stop and ask the user.
5. **Regenerate the traceability report** with `make traceability`. Every requirement in completed phases must be covered.
6. **Write the checkpoint report** `docs/checkpoints/P<n>-<name>.md` using Appendix 2.
7. **Tag a pre-release** `v0.<n>.0` with `gh release create --prerelease`, using the checkpoint report as the release notes and attaching the benchmark JSON.
8. **Post the report summary to the user in chat and STOP.** Don't begin the next phase until the user approves. Apply any feedback first, as a follow-up PR, and note it in the report.

---

## Part E — Phase-by-Phase Module Guide

```text
1. P0 Foundations
   repo, tooling, core interfaces, mock upstream
   CI + benchmark plumbing before any features
   → Checkpoint 0

2. P1 Proxy  →  P2 Reliability  →  P3 Performance
   gateway hot path built up layer by layer
   each layer proven by functional + chaos tests
   → Checkpoints 1, 2, 3

3. P4 Visibility  →  P5 Incidents
   telemetry storage, dashboard API, React UI
   ASK USER frontend questions before M4.7
   → Checkpoints 4, 5

4. P6 AI  →  P7 Self-host polish
   provider-agnostic AI on top of clean data
   fresh-VM install + release v1.0.0
   → Checkpoints 6, 7
```

Each module lists: **Spec** references · **Build** steps · **Tests** by type · **DoD** (Definition of Done). In addition to what each module lists, every module's DoD includes: CI green, coverage gates met, docs updated, PROGRESS.md updated.

---

### Phase 0 — Foundations

#### M0.1 — Repository scaffold and tooling
**Spec:** §16.1
**Build:**
- Monorepo layout per spec, with `uv` workspace (`packages/core`, `services/gateway`, `services/api`, `services/worker`, `tools/mock_upstream`).
- Root `pyproject.toml`: ruff, mypy (strict for core and gateway), pytest config and markers (§B.2), coverage config.
- `pre-commit` hooks: ruff, mypy, gitleaks, end-of-file fixes.
- `Makefile` with all targets from §B.2 (placeholders allowed until their suites exist).
- `.github/`: PR template (module ID, spec refs, tests added by type, benchmark impact, docs updated), issue templates (bug, task), CODEOWNERS.
- `docs/PROGRESS.md`, `docs/adr/0001-record-architecture-decisions.md`, plus one ADR per locked decision D1–D12.

**Tests:**
- Unit: a trivial smoke test per package, proving discovery and markers work.
- Meta: CI fails if a test lacks a type marker (a pytest plugin hook in `conftest.py`).

**DoD:** `make test-unit` passes locally and in CI. Pre-commit runs clean.

#### M0.2 — Core package: models, interfaces, errors, requirements
**Spec:** §4.4, §5.1, §16.2, A.3
**Build:**
- Pydantic v2 models for `ApiDependency` config (§5.1) with validation, including cross-field rules: `attempt_timeout_ms ≤ deadline_ms`, `cap_ms ≥ base_ms`, `0 < budget_ratio ≤ 1`, valid credential-mode fields.
- The normalized internal request object and the `AuthContext`.
- Interfaces: `CacheStore`, `RateLimiter`, `CircuitStore`, `Deduplicator`, `TelemetrySink`, `ConfigSource`, `SecretBox`, `LLMProvider`, `Clock`, `Random`. Each gets an in-memory implementation.
- `JarvisError` hierarchy mapping to the `JV_*` codes and HTTP statuses.
- `docs/requirements.md`: extract every testable requirement from the spec with REQ-IDs (§B.4).
- Traceability generator script (`scripts/traceability.py`).

**Tests:**
- Unit: config validation, both valid and invalid, with property-based tests (hypothesis) for numeric ranges; error-to-HTTP mapping; in-memory implementations satisfy the interface contracts.
- **Contract test suites:** reusable abstract test classes per interface (e.g. `CacheStoreContract`). Every future implementation (Redis, Postgres) must subclass and pass the same suite.

**DoD:** requirements list committed and complete for every spec section. Contract suites pass for the in-memory implementations.

#### M0.3 — Compose skeleton and health
**Spec:** §15.1
**Build:**
- Minimal FastAPI apps for gateway, api and worker, each with `/healthz` (liveness) and `/readyz` (dependencies reachable).
- Dockerfiles: multi-stage, non-root user, slim base images, pinned versions.
- `deploy/docker-compose.yml` with the default profile, plus `docker-compose.dev.yml` (hot reload) and a `test` profile (with the mock upstream included, and ports exposed for tests).
- Health checks and `depends_on: condition: service_healthy`.
- `.env.example` documenting every variable.

**Tests:**
- System: `tests/system/test_stack_health.py`. The stack comes up and every `/readyz` returns 200 within 60 s.
- Chaos (first instance): stop Redis, `/readyz` of the dependent services reports degraded, restart Redis, and they recover.
- Security: containers run as non-root (inspect the image config).

**DoD:** `docker compose up -d` gives an all-healthy stack on a clean machine.

#### M0.4 — CI workflows
**Spec:** §16.3
**Build:**
- `ci.yml`: lint, type-check, unit tests, integration tests (Postgres/Timescale and Redis as service containers), coverage gates, job summary.
- `system.yml`: compose-based system and functional tests (runs on the `system` label and on main).
- `security.yml`: bandit, pip-audit, gitleaks, trivy image scan (fail on HIGH/CRITICAL with a fix available). Runs on PRs and weekly.
- Caching for uv and Docker layers.
- Branch protection on `main` with required checks (document the settings in `docs/contributing.md`; the user applies them in the GitHub UI).

**Tests:** trigger each workflow at least once. Deliberately open a PR with a failing test to prove the gate blocks the merge, then close it.

**DoD:** all workflows are green on `main`, and the gate is proven to block.

#### M0.5 — Mock upstream (test asset)
**Build:** `tools/mock_upstream/`, a FastAPI app with:
- A catch-all route that behaves according to rules set via the admin API: fixed or random latency, error rate by status, `429`/`503` with `Retry-After`, connection reset, hang (never responds), oversized body, `Set-Cookie`, `Cache-Control`, echoing received headers and query, and redirects to arbitrary URLs.
- An admin API: `POST /__admin/rules`, `GET /__admin/calls` (count plus the recorded headers and query of each call, for asserting what JARVIS sent), `POST /__admin/reset`.
- Deterministic mode (seeded) for tests.

**Tests:** unit and integration tests for the mock itself. It has to be trustworthy, since every later test depends on it.

**DoD:** documented in `tools/mock_upstream/README.md` and packaged in the compose `test` profile.

#### M0.6 — Benchmark infrastructure
**Spec:** §13 · **Guide:** Part C
**Build:**
- `benchmarks/budgets.yaml`, generated from spec §13.
- pytest-benchmark configuration and one example micro-benchmark.
- k6 scaffolding: shared config, the direct-vs-proxied overhead helper, an `env.json` writer, and `k6_to_gab.py`.
- `check_budgets.py`, and `make bench-checkpoint` (runs everything and writes the checkpoint JSON plus `BENCHMARKS.md`).
- `bench.yml` with github-action-benchmark → `gh-pages` (enable Pages).
- A `docker stats` sampler for footprint and soak tests.

**Tests:**
- Unit: the converter and the budget checker, using fixture inputs.
- A dry run against the mock upstream directly, proving the whole path: job summary → artifact → gh-pages chart.

**DoD:** the gh-pages benchmark page exists and shows at least one data point. `BENCHMARKS.md` exists with its column structure.

#### ✅ Checkpoint 0
**Acceptance stories:**
- *Given* a clean machine, *when* I clone and run `docker compose up -d`, *then* all services report healthy.
- *Given* a PR with a failing test, *when* CI runs, *then* the merge is blocked.
- *Given* a push to main, *when* `bench.yml` runs, *then* a data point appears on the gh-pages benchmark page.

Run the Part D protocol and stop.

---

### Phase 1 — Proxy

#### M1.1 — Database schema and migrations
**Spec:** §12 (excluding the Timescale tables, which come in M4.1)
**Build:**
- SQLAlchemy 2.0 async models and Alembic migrations for `users`, `projects`, `jarvis_keys`, `api_dependencies`, `api_policy_versions`, `secrets`, `settings`, `audit_log`.
- Migrations run automatically at `api` startup, protected by a Postgres advisory lock.
- Repository classes behind the `ConfigSource` interface.

**Tests:**
- Integration: upgrade from an empty database to head, downgrade back to base, and upgrade again; constraints (unique slugs per project, foreign keys); concurrent startup of two `api` instances runs migrations exactly once.
- Unit: repository mapping logic.

**DoD:** migrations are reversible, and the advisory lock is proven by the concurrency test.

#### M1.2 — SecretBox (encryption)
**Spec:** §11.2
**Build:**
- AES-256-GCM encryption with a random nonce per record, storing a key ID with each record.
- Master key loaded from `JARVIS_MASTER_KEY`.
- A `jarvis rotate-master-key` CLI command.

**Tests:**
- Unit: round-trip; tampering with the ciphertext or nonce fails authentication; decrypting with the wrong key fails; two encryptions of the same plaintext produce different ciphertexts.
- Integration: rotation re-encrypts every record, the old key can no longer decrypt, and the new key can.
- Security: `repr`/`str` of secret objects never reveal the plaintext; a log-capture test asserts the plaintext never appears in logs.

**DoD:** rotation is documented, and the security tests pass.

#### M1.3 — Minimal management API (admin token)
**Spec:** §10 (projects, keys, APIs, `/apis/{id}/test` placeholder)
**Build:**
- CRUD for projects, JARVIS keys, and API dependencies, authenticated with an admin token from the environment for now (sessions come in M4.3).
- JARVIS keys are generated as `jv_live_<32 bytes base62>`, stored as SHA-256 hashes, and the full key is returned exactly once.
- Provider secrets are stored through SecretBox and never returned by any endpoint.
- Every save of an API creates a policy version row and publishes `config:changed`.

**Tests:**
- Unit: key generation format and entropy, hashing.
- Integration: CRUD round-trips; secrets are absent from every response; `config:changed` is published on save.
- Functional (REQ-10.x): validation errors return 422 with field paths.
- Regression: the OpenAPI contract snapshot is created now.

**DoD:** the API is usable via `curl` or httpie, following `docs/quickstart-dev.md`.

#### M1.4 — Gateway config loader
**Spec:** §5.2, §8
**Build:**
- An in-memory `ConfigStore` holding policies and key hashes, swapped atomically (immutable snapshot objects).
- Load at startup: Postgres first; if Postgres is unreachable, fall back to the local snapshot file.
- Write the snapshot file after every successful load.
- A pub/sub subscriber for `config:changed` and key revocation.
- A full resync every 60 s.

**Tests:**
- Unit: atomic swap (concurrent readers never see a mixed state); snapshot serialization round-trip.
- Integration: an edit via the API reaches the gateway in ≤ 1 s (measured); a missed pub/sub message is corrected by the resync (using a fake clock or a shortened interval); startup with Postgres down loads from the snapshot; startup with Postgres down *and* no snapshot fails with a clear log message and a non-ready status.
- Benchmark: `config_propagation_p99` is recorded.

**DoD:** propagation within budget, confirmed in the integration test.

#### M1.5 — Gateway authentication
**Spec:** §4.2
**Build:**
- Look for the JARVIS key in `X-Jarvis-Key` first, then in the API's configured credential slot (by `jv_` prefix).
- Resolve it to an `AuthContext`, checking project scope and allowed APIs.
- Revoked keys are rejected within 1 s of revocation.

**Tests:**
- Unit: extraction precedence; malformed keys; a slot value that doesn't start with `jv_` is treated as no key; comparisons are constant-time.
- Functional: valid, invalid, revoked, wrong-project and disallowed-API cases each return the right `JV_AUTH_INVALID` response.
- Security: the key is never forwarded upstream (assert via mock upstream `/__admin/calls`); the key never appears in logs or telemetry.

**DoD:** every REQ-4.2 requirement is covered.

#### M1.6 — Path templating
**Spec:** §4.14
**Build:**
- Match explicit patterns first; otherwise apply the automatic numeric, UUID and long-hex/base64 fallback.
- Per-API cardinality cap with an `{other}` overflow bucket.

**Tests:**
- Unit: table-driven cases; hypothesis property tests (templating never raises, output is stable for the same input, the cap holds under random paths).
- Micro-benchmark: `route_template_match`.

**DoD:** the benchmark is logged and within a reasonable per-request cost (< 20 µs).

#### M1.7 — SSRF guard
**Spec:** §4.13
**Build:**
- Validate the host at config save time.
- At connect time, a custom httpx transport resolves DNS, validates every resolved IP, and pins the connection to the validated IP while preserving SNI and the `Host` header.
- Default block list from the spec, plus the admin allowlist.
- Redirects are not followed by default.
- Plain `http` upstreams are allowed only for allowlisted hosts.

**Tests:**
- Unit: an IP classification table covering IPv4, IPv6, IPv4-mapped IPv6, decimal and octal IP forms, and `0.0.0.0`.
- Integration: DNS rebinding simulation (a resolver stub that returns a public IP at save time and a private one at connect time) is blocked; a redirect to `169.254.169.254` is blocked when redirects are enabled; allowlisted internal hosts work.
- Security suite: `tests/security/test_ssrf.py` covering all bypass forms. Kept permanently as regression tests.

**DoD:** the SSRF suite is green, and each blocked attempt returns `JV_DESTINATION_BLOCKED`.

#### M1.8 — Forwarding, credential injection, header hygiene
**Spec:** §4.1, §4.3 (steps 1, 2, 8 without retries, 10), §4.4, §4.5
**Build:**
- One pooled `httpx.AsyncClient` per upstream host.
- Forward the method, path, query and body.
- Strip hop-by-hop headers (`Connection`, `Keep-Alive`, `Transfer-Encoding`, `TE`, `Upgrade`, `Proxy-*`); rewrite `Host`.
- Strip the credential slot, then inject the real credential by mode.
- Enforce the body size limit and reject streaming requests.
- Add the `X-Jarvis-*` response headers, and produce JARVIS errors in the standard JSON format.
- Assign a ULID `request_id` to every request.

**Tests:**
- Unit: header filtering; injection for each mode; building the query string when injecting a query credential while preserving existing parameters.
- Functional: a table-driven suite covering methods × credential modes × body types, asserting exactly what the upstream received (via mock upstream call recording); 4xx/5xx from the upstream pass through byte-for-byte; oversized bodies are rejected with 413 and `JV_BODY_TOO_LARGE`; streaming requests are rejected.
- System: an example client using a real SDK-style base-URL override (a small `requests`-based client) works unchanged except for the base URL and key.

**DoD:** the "change one base URL" story passes end to end.

#### M1.9 — Telemetry emitter
**Spec:** §6.1, §6.3
**Build:**
- A bounded in-memory queue with batched, pipelined `XADD` into a stream with `MAXLEN ~`.
- A `telemetry_dropped_total` counter.
- Emitting never blocks the request path.

**Tests:**
- Unit: event construction for every outcome; queue overflow drops events and increments the counter.
- Integration: events land in the stream with the exact schema; with Redis down, requests still succeed and the drop counter increases.
- Regression: the telemetry schema snapshot is created.
- Performance: an A/B micro-benchmark with telemetry on and off shows < 0.5 ms p99 difference.

**DoD:** it's proven that telemetry never adds blocking latency.

#### ✅ Checkpoint 1
**Acceptance stories:**
- *Given* a project, a key, and an API pointing at the mock upstream, *when* a client changes only its base URL and key, *then* requests succeed and the upstream receives the real credential and never the JARVIS key.
- *Given* an API with base URL `http://169.254.169.254`, *when* I save it, *then* it is rejected.
- *Given* a key revoked via the API, *when* the next request arrives 1 s later, *then* it is rejected.
- *Given* Postgres is down and a snapshot exists, *when* the gateway restarts, *then* it serves traffic.

Benchmarks in scope: overhead (S2 only, since there's no cache yet), `config_propagation_p99`, micro-benchmarks. Run the Part D protocol and stop.

---

### Phase 2 — Reliability

#### M2.1 — Deadlines and attempt timeouts
**Spec:** §4.6
**Build:**
- A `Deadline` object (from the injected `Clock`) carried through the pipeline.
- A per-attempt timeout computed as `min(attempt_timeout, remaining deadline)`.
- No new attempt starts when the remaining time is below `min_attempt_ms`.

**Tests:**
- Unit (fake clock): budget arithmetic at the edge values.
- Functional: an upstream that hangs is cut off at the attempt timeout; total latency never exceeds deadline + ε (assert ε ≤ 50 ms); the error code is `JV_UPSTREAM_TIMEOUT`.

**DoD:** no request can exceed its deadline. This holds across a hypothesis-generated set of latency sequences run against the mock upstream.

#### M2.2 — Retry policy
**Spec:** §4.7
**Build:**
- The retryability decision based on condition × method × idempotency configuration.
- Full-jitter backoff using the injected `Random`.
- `Retry-After` honored if it fits within the remaining deadline.
- An idempotency key is generated once per request and reused on every attempt.

**Tests:**
- Unit: a decision table (every status and error condition × every method × idempotency on/off); backoff bounds (hypothesis: the delay is always in `[0, min(cap, base·2^n)]`); `Retry-After` parsing in both seconds and HTTP-date forms.
- Functional: 503-then-200 succeeds with `X-Jarvis-Attempts: 2`; POST with no idempotency config is never retried (the mock upstream sees exactly 1 call); POST with idempotency config sends the same key on every attempt; a 4xx is never retried.
- Micro-benchmark: `backoff_compute`.

**DoD:** every REQ-4.7 requirement is covered.

#### M2.3 — Retry budget
**Spec:** §4.7 (budget)
**Build:** a Redis sliding-window counter of requests and retries per API. When the ratio exceeds `budget_ratio`, retries are refused and a `retry_budget_exhausted` event is emitted.

**Tests:**
- Integration: with the upstream returning 100% 503 under load (S4), the observed retry ratio stays ≤ budget + tolerance; the budget is shared across two gateway instances.
- Non-functional (retry-storm test): upstream calls per client request stay ≤ 1 + budget_ratio.

**DoD:** the retry-storm test passes.

#### M2.4 — Circuit breaker
**Spec:** §4.8
**Build:**
- A pure state machine (unit-testable with a fake clock).
- A Redis `CircuitStore`, with a Lua script for atomic transitions and a Redis lock for the single half-open probe.
- A local mirror in each gateway, refreshed every 1 s and on pub/sub events.
- Exponential growth of the open duration, capped.

**Tests:**
- Unit: every transition, including the `min_calls` floor, 4xx not counted, probe success and failure, doubling and the cap. Hypothesis: a random sequence of outcomes never reaches an illegal state.
- Integration: two gateway instances see the same state; exactly one probe is sent while half-open, even with 50 concurrent requests (assert via mock upstream call count).
- Functional: while OPEN, requests fail fast with `JV_CIRCUIT_OPEN` and the upstream receives 0 calls.
- Benchmark: `circuit_open_fail_fast_p99`.

**DoD:** the single-probe guarantee is proven under concurrency.

#### M2.5 — Rate limiters
**Spec:** §4.11
**Build:**
- A token bucket implemented as a Redis Lua script (refill and take in one atomic call).
- An inbound limiter (per project, key and API) and an upstream quota limiter (per API), placed at pipeline steps 3 and 7.
- Responses of 429 with `Retry-After`.
- An in-memory fallback bucket.

**Tests:**
- Unit (in-memory bucket, fake clock): refill math, burst capacity, fractional refill rates.
- Contract: the Redis implementation passes the same `RateLimiter` contract suite.
- Integration: fairness across three gateway instances (the total admitted count is within capacity + ε); concurrency (hundreds of simultaneous takes never over-admit).
- Functional: the inbound limiter uses separate buckets for separate keys; the upstream limiter applies across keys.
- Micro-benchmark: `token_bucket_take`.

**DoD:** there is zero over-admission in the concurrency test.

#### M2.6 — Fallback (API fallback)
**Spec:** §4.12 (item 2 and the controlled error; `serve_stale` comes in M3.2)
**Build:** `fallback_api` re-runs the pipeline from step 6 against the fallback API. Chains are prevented at config validation time.

**Tests:**
- Unit: chain detection in config validation.
- Functional: with the primary circuit OPEN, the request is served by the fallback API with `X-Jarvis-Source: fallback`; when both fail, a controlled error is returned.

**DoD:** every REQ-4.12 requirement (except `serve_stale`) is covered.

#### M2.7 — Redis-down fail-open mode
**Spec:** D3, §8
**Build:**
- Detect Redis failure (a circuit breaker on the Redis client itself, so a down Redis doesn't slow every request).
- While Redis is down: use the in-memory limiter, the local circuit store, and skip cache and dedup.
- Recover automatically when Redis returns.

**Tests:**
- Chaos: run S8 (S5 while Redis is killed and restored). The error rate caused by JARVIS stays at 0; p99 during the outage is ≤ 2× the normal p99; the stack recovers within 5 s of Redis returning.
- Functional: `/readyz` reports degraded mode, and a `redis_unavailable` telemetry event is emitted after recovery.

**DoD:** the chaos test passes and the results are logged as a benchmark.

#### ✅ Checkpoint 2
**Acceptance stories:**
- *Given* an upstream failing 60% of the time, *when* traffic continues, *then* the circuit opens, requests fail fast, one probe tests recovery, and the circuit closes once the upstream is healthy.
- *Given* a POST without an idempotency config, *when* the upstream returns 503, *then* JARVIS does not retry.
- *Given* a quota of 60/min, *when* 200 requests arrive in a minute, *then* the upstream receives ≤ 60.
- *Given* Redis is killed during load, *when* traffic continues, *then* no request fails because of JARVIS.

Benchmarks in scope: S2, S4, S8, retry-storm ratio, micro-benchmarks. Run the Part D protocol and stop.

---

### Phase 3 — Performance

#### M3.1 — Cache
**Spec:** §4.9
**Build:**
- Eligibility rules; cache-key construction (method, route path, sorted query, vary headers).
- TTL per route, with an optional `Cache-Control` override.
- A stale window.
- Size limits; manual invalidation (per API, per route, all).

**Tests:**
- Unit: an eligibility table (method × status × `Set-Cookie` × size × `Cache-Control`); key determinism and query-order independence; vary headers produce different keys; hypothesis: the same logical request always yields the same key.
- Contract: the Redis `CacheStore` passes the contract suite.
- Functional: the second identical GET returns `X-Jarvis-Cache: HIT` and the upstream is called once; `Set-Cookie` responses and 5xx responses are never cached; `no-store` is honored when the toggle is on; invalidation works.
- Security: responses for different projects never collide (a test with identical paths in two projects).
- Micro-benchmark: `cache_key_build`.

**DoD:** every REQ-4.9 requirement is covered, and there are no cross-project leaks.

#### M3.2 — Serve-stale fallback
**Spec:** §4.12 (item 1)
**Tests:** functional: after the TTL expires and while the upstream is down, the request returns `STALE` with the old body; after the stale window expires, it returns a controlled error.
**DoD:** the full fallback order from spec §4.12 is proven.

#### M3.3 — In-process singleflight
**Spec:** §4.10
**Build:** a map from cache key to `asyncio.Future`; the leader resolves or rejects the future; followers await with their own deadline; cleanup happens on every path, including exceptions.

**Tests:**
- Unit: 100 concurrent coroutines → 1 upstream call; the leader's exception propagates to followers; cancellation of the leader doesn't leak futures (assert the map is empty afterwards).
- Soak: no growth of the map under S3 repeated for 10 minutes.

**DoD:** zero leaked entries under the soak test.

#### M3.4 — Cross-instance deduplication
**Spec:** §4.10
**Build:** `SET inflight:{key} NX PX`; followers subscribe to `done:{key}` and then read from the cache; if the leader dies, the lock expires and followers fall back to calling the upstream.

**Tests:**
- Integration: S3 against three gateway instances results in 1 upstream call. Killing the leader's gateway mid-request means followers still complete within their deadline.
- Benchmark: `dedup_upstream_calls_per_1000_identical` (target 1).

**DoD:** the dedup benchmark is logged at target.

#### M3.5 — Redis round-trip consolidation
**Spec:** §4.3 (≤ 2 round trips before upstream, ≤ 1 after)
**Build:**
- Merge the checks for pipeline steps 3–7 into Lua scripts, keeping the logic identical to the separate implementations.
- An instrumented Redis client that counts round trips per request (enabled in tests and benchmarks only).

**Tests:**
- Regression: the entire P2 and P3 functional suites pass unchanged with the consolidated scripts.
- Performance: `gw_redis_roundtrips_pre` ≤ 2 and `gw_redis_roundtrips_post` ≤ 1, asserted in tests.

**DoD:** round-trip budgets enforced by tests.

#### M3.6 — Performance hardening and full gateway load suite
**Spec:** §13
**Build:**
- uvloop, orjson responses, connection pool tuning, and worker count guidance.
- Profile with py-spy under S1/S2 and fix hotspots. Commit flamegraphs to `benchmarks/profiles/P3/`.
- Implement scenarios S1–S8 in full.

**Tests:**
- Non-functional: all gateway budgets in §13 are met in `make bench-checkpoint`; S6 records the breaking point; S7 (2 h soak) shows an RSS slope ≤ 5 MB/h.
- Mutation testing on the core algorithms (§B.3).

**DoD:** every §13 gateway budget is green in `BENCHMARKS.md`. If one is missed, stop and ask the user before any architectural change.

#### ✅ Checkpoint 3
**Acceptance stories:**
- *Given* 1,000 identical concurrent GETs, *then* the upstream receives 1 call and every caller gets the correct body.
- *Given* a cached endpoint whose upstream then goes down, *then* callers get stale data inside the stale window.
- *Given* the S5 mixed workload, *then* the dashboard-ready metric `upstream_calls_avoided_ratio` is reported.

The checkpoint report must include the flamegraph findings, the mutation score, and the S6 breaking point. Run the Part D protocol and stop.

---

### Phase 4 — Visibility

Build the backend first (M4.1–M4.6). **Stop before M4.7** and ask the user the frontend questions in Appendix 1.

#### M4.1 — Timescale storage and retention
**Spec:** §6.2, §12
**Build:**
- Migrations for the `request_events` and `gateway_events` hypertables.
- Continuous aggregates `metrics_1m` and `metrics_1h` (with percentile sketches), plus refresh policies.
- Retention policies from environment variables.
- Indexes to support the request log view.

**Tests:**
- Integration: insert synthetic events, refresh, and check that aggregates match a Python reference calculation (counts exactly, percentiles within sketch tolerance); retention drops old chunks; a migration regression test from the P3 schema with seeded data.
- Performance: aggregate queries over 30 days of synthetic data (generated at the scale of 1M requests/day) return in < 100 ms.

**DoD:** aggregate correctness is proven against the reference.

#### M4.2 — Worker ingestion
**Spec:** §6.1–6.3, §8
**Build:**
- A Redis Streams consumer group with batched inserts (COPY or multi-row insert).
- Idempotency via `request_id` uniqueness, so redelivery never double-counts.
- Pending-entry reclaim after a worker crash.
- An `ingest_lag` metric.

**Tests:**
- Integration: at-least-once delivery produces exactly-once rows (kill the worker mid-batch, restart, and compare counts); a poison message goes to a dead-letter stream without blocking the consumer.
- Chaos: with Postgres down, the worker pauses and the stream buffers; after Postgres returns, the backlog drains.
- Performance: `ingest_lag_p99` under S5 load is ≤ 2 s.

**DoD:** the exactly-once row test passes, and the lag benchmark is logged.

#### M4.3 — Dashboard authentication, roles, and audit
**Spec:** §11.1, §10 (auth, setup, users, audit)
**Build:**
- Argon2id password hashing and server-side sessions (Redis, with Postgres fallback).
- `HttpOnly`, `Secure`, `SameSite=Lax` cookies; CSRF double-submit token; login rate limiting.
- The `/setup` flow, allowed only while no users exist.
- `admin` and `member` roles enforced by a dependency on every route.
- An audit log writer for every mutating action.
- The admin token from M1.3 is kept only for automation and marked as such.

**Tests:**
- Unit: permission matrix (every route × role).
- Functional: setup runs once and a second call gets 409; login, logout, session expiry; CSRF-less mutations are rejected; members get 403 on config changes; every mutation produces an audit row.
- Security: brute-force lockout, session fixation (the session ID rotates at login), cookie flags present.

**DoD:** the permission matrix test covers 100% of routes. This is enforced by a test that enumerates the app's routes.

#### M4.4 — Metrics endpoints and live SSE
**Spec:** §10 (metrics, live), §13 (dashboard API)
**Build:**
- Overview, API detail, route list and request log endpoints (cursor-paginated), reading only from rollups, except the request log.
- `/stream/live` over SSE, with heartbeats, reconnect support via `Last-Event-ID`, and pushes of health, incident and summary updates every 2 s.

**Tests:**
- Functional: numbers match a seeded fixture exactly.
- Performance: `dash_api_p95_*` ≤ 200 ms with 30 days of data at the M4.1 scale; SSE delivers an update ≤ 2 s after the event; 50 concurrent SSE clients don't degrade the gateway (its S1 p99 is unchanged within noise).
- Regression: OpenAPI snapshot updated.

**DoD:** dashboard API budgets are green.

#### M4.5 — Prometheus endpoints and monitoring profile
**Spec:** §6.4, §15.1
**Build:** `/metrics` on every service; the `monitoring` compose profile with Prometheus and Grafana, and a provisioned JARVIS dashboard.

**Tests:** system: with the profile up, Prometheus scrapes all targets (query the targets API); without the profile, nothing breaks.

**DoD:** the monitoring profile is optional and works.

#### M4.6 — Web-facing API hardening
**Build:**
- Generate an OpenAPI-based TypeScript client (`openapi-typescript` + a fetch wrapper) into `web/src/api/`.
- Consistent error envelopes.
- CORS locked to same-origin (Caddy serves both).

**Tests:** contract: the generated client compiles, and CI fails if the client is stale relative to the OpenAPI.

**DoD:** the frontend can be built entirely against typed endpoints.

#### ⛔ STOP — Frontend Discovery

Before M4.7:

1. Ask the user **every** question in Appendix 1, in one message, and wait for the answers.
2. Build **two or three static visual directions** as a throwaway Vite page on branch `design/directions`, based on their answers: the Overview page in both light and dark, using realistic fake data. Share screenshots and how to run the page, then ask the user to pick one direction or ask for changes.
3. Record the chosen direction in `docs/adr/00xx-ui-design-direction.md` and in `web/DESIGN.md`: design tokens (color, type scale, spacing, radius, shadows, motion), component rules, chart styling, and do's and don'ts.
4. Only then continue to M4.7. Every later UI PR must follow `web/DESIGN.md`.

#### M4.7 — Web scaffold and design system
**Spec:** §14
**Build:**
- React + TypeScript (strict) + Vite, Tailwind CSS with the design tokens from `DESIGN.md`, and shadcn/ui components themed to those tokens.
- TanStack Query and React Router.
- A light/dark theme toggle, defaulting to whatever the user chose.
- Layout shell (sidebar, header), the ⌘K command palette, skeleton loaders, toasts, and an error boundary.
- A Caddy image that serves the built assets and proxies `/api`.

**Tests:**
- Unit/component (Vitest + Testing Library): theme toggle persistence, command palette keyboard navigation, the error boundary.
- Accessibility: axe on the shell in both themes, with no serious or critical violations.
- Visual: Playwright screenshot baselines for the shell.
- Performance: Lighthouse CI budgets set (perf ≥ 90, a11y ≥ 95, initial JS ≤ 250 KB gzipped unless the user agrees otherwise).
- A component catalog page (`/__catalog`, dev only) showing every component in every state, for design review.

**DoD:** the shell matches `DESIGN.md`, and the user has approved screenshots of the catalog.

#### M4.8 — Setup and login pages
**Tests:**
- E2E (Playwright): first-run setup → login → logout → login again; wrong password shows the error state; the lockout message is shown.
- Accessibility and visual baselines.

**DoD:** the onboarding flow is complete in the UI.

#### M4.9 — Overview page
**Build:**
- Headline stats (requests, upstream calls avoided, error rate, cache hit ratio), API health grid with sparklines, open incidents strip.
- Live updates via SSE, with smooth number transitions and no layout shift.

**Tests:**
- Component: stat formatting (large numbers, percentages, empty states).
- E2E: generate traffic via the gateway, and the numbers update within 2 s without a page reload.
- Visual: light, dark, and empty states.
- Performance: Lighthouse on Overview meets the budgets; no long tasks > 50 ms during live updates (Playwright performance trace).

**DoD:** meets `DESIGN.md` and the performance budgets.

#### M4.10 — APIs list and API detail
**Build:**
- The APIs list; the API detail tabs Metrics, Routes, Config, and Versions (read-only list for now).
- A form-based config editor: every field from §5.1, with inline validation that mirrors the backend rules, per-field help text, and a diff preview before saving.
- The "Test request" pipeline trace view, backed by `/apis/{id}/test`.

**Tests:**
- Component: form validation parity. The same invalid fixtures are rejected by both the frontend and backend. Shared fixtures are generated from the Pydantic JSON schema.
- E2E: add an API through the UI → test request → call it through the gateway → metrics appear; edit the TTL → the gateway picks it up in ≤ 1 s.
- Visual and accessibility (forms: labels, error announcements, keyboard-only completion).

**DoD:** an API can be onboarded entirely through the UI.

#### M4.11 — Settings pages
**Build:** projects and keys (the key is shown once, with a copy button), users and roles, SSRF allowlist, retention, fail-open mode, audit log viewer (virtualized).

**Tests:** E2E per page; role-based UI (members don't see admin actions, *and* the backend rejects them anyway); the virtualized audit log stays smooth with 100k rows (performance trace).

**DoD:** every §10 settings endpoint is reachable from the UI.

#### ✅ Checkpoint 4
**Acceptance stories (Playwright, full stack):**
- *Given* a fresh install, *when* I complete setup, create a project and a key, and add an API through the UI, *then* my app can call it through the gateway and I see live metrics within 2 s.
- *Given* a member account, *then* I can view everything but cannot change configuration.
- *Given* the dashboard is open during S5 load, *then* the UI stays responsive (no long tasks > 50 ms) and gateway budgets are unchanged.

Include light and dark screenshots of every page in the checkpoint report. Run the Part D protocol and stop.

---

### Phase 5 — Incidents

#### M5.1 — Rules engine
**Spec:** §7.1, §7.2
**Build:**
- A pure, deterministic rule evaluation function that takes the window metrics and baseline, and returns the rules that fired.
- Baseline computation (a 24 h median excluding incident windows, with absolute thresholds as a fallback).
- A 60 s evaluation loop in the worker.

**Tests:**
- Unit: each rule at boundary values; `min_samples` suppresses noise; the sustain counter works; the fallback thresholds apply with less than 24 h of history.
- Scenario tests: replay recorded synthetic traces (a latency spike, a slow ramp, one-off blips, low traffic) and assert exactly which rules fire. Traces live in `tests/fixtures/traces/` and serve as regression assets.

**DoD:** zero false positives on the low-traffic and blip traces.

#### M5.2 — Incident lifecycle
**Spec:** §7.3
**Build:** open, update and resolve incidents, with one open incident per API; the timeline; severity; SSE pushes.

**Tests:**
- Unit: the lifecycle state machine.
- Integration: an injected upstream failure (mock upstream at 30% 503 for 5 min) opens exactly one incident, which resolves 5 min after recovery (shortened intervals in the test profile).
- Chaos: restarting the worker during an open incident doesn't create a duplicate.

**DoD:** exactly one incident per failure episode.

#### M5.3 — Config versioning, diff, and rollback
**Spec:** §5.2
**Build:** structured diffs between versions, a rollback endpoint that creates a new version, and a required reason field.

**Tests:**
- Unit: diff correctness for nested config.
- Functional: rollback restores the old behavior at the gateway within 1 s; history is never rewritten; audit rows are created.

**DoD:** every REQ-5.2 requirement is covered.

#### M5.4 — Incidents UI and Versions UI
**Build:** incident list and detail (timeline, impacted-metrics chart with the incident window shaded, a reserved slot for AI explanation that says "Configure AI" for now); version history with a side-by-side diff and a rollback action behind a confirmation.

**Tests:** E2E: an injected failure → the incident appears live → resolve → the timeline is complete; rollback through the UI. Visual and accessibility baselines.

**DoD:** incidents are fully usable without AI.

#### ✅ Checkpoint 5
**Acceptance stories:**
- *Given* an upstream that degrades for 5 minutes, *then* one incident opens, shows a correct timeline, and resolves on its own.
- *Given* one-off blips on a low-traffic API, *then* no incident opens.
- *Given* a bad config change, *then* I can roll it back from the UI and the gateway follows within 1 s.

Run the Part D protocol and stop.

---

### Phase 6 — AI

#### M6.1 — Provider interface and fake provider
**Spec:** §9.2
**Build:**
- The normalized message, tool and streaming event schema in `packages/core`.
- A `ScriptedProvider` for tests: it replays scripted tool calls and text, so the AI flows can be tested without network access.
- A `ProviderRegistry` that reads settings and hot-swaps the provider.

**Tests:** unit: schema validation; the registry swaps providers mid-run without dropping in-flight jobs; `provider=none` disables every AI entry point cleanly.

**DoD:** all AI features can be developed and tested offline.

#### M6.2 — Anthropic adapter · M6.3 — Gemini adapter
**Build:**
- Translate the internal schema to and from each provider's format: tool definitions, tool calls and results, streaming events, and errors (auth, rate limit, overloaded, context too long) mapped to internal error types.
- Timeouts and retries using the P2 retry policy code.

**Tests:**
- Unit: translation round-trips using **recorded fixture responses** (captured once, committed with no secrets).
- Contract: both adapters pass the same `LLMProviderContract` suite.
- Live smoke tests (`@pytest.mark.live`): one minimal tool-calling conversation per provider. These run only in `bench.yml`/`ai-eval.yml` with secrets and never on forks.

**DoD:** the contract suite passes for both adapters, and the live smoke tests pass in the nightly run.

#### M6.4 — Redaction layer
**Spec:** §9.6
**Build:** an allowlist-based serializer. Only whitelisted fields in tool outputs and evidence bundles can pass, and it runs as the last step before any provider call.

**Tests:**
- Unit: every tool output schema run through redaction with canary secrets and PII in every field.
- Security (permanent regression): a canary test seeds a project with canary credentials, a canary JARVIS key, and a canary body sample. It runs the explanation, chat and recommendation flows with a *recording* provider and asserts that no canary string appears in any captured provider request.
- Micro-benchmark: `redaction_apply`.

**DoD:** the canary suite passes.

#### M6.5 — Tools and registry
**Spec:** §9.4
**Build:** every tool in the spec, implemented over the existing repositories and metrics queries. Enforce the limits: 8 calls, 8 KB per result, 90 s total.

**Tests:**
- Unit: each tool's output schema and the truncation behavior.
- Integration: tools return correct data against seeded fixtures.
- Security: no tool can mutate config, as shown by static enumeration of the tool registry plus a test.

**DoD:** every tool is documented with its schema in `docs/ai-tools.md`.

#### M6.6 — Evidence bundle builder
**Build:** deterministic assembly per §9.4, with size limits and a stable ordering.

**Tests:**
- Unit: snapshot tests on the P5 trace fixtures (the same inputs always produce the same bundle).
- Micro-benchmark: `evidence_bundle_build`.

**DoD:** bundles exist for every trace fixture.

#### M6.7 — Explanation and summary jobs
**Spec:** §9.3
**Build:** worker jobs triggered by incident open and resolve; results stored and never regenerated on view; a "Regenerate" endpoint; retries with backoff when the provider fails; clear failure states.

**Tests:**
- Integration (ScriptedProvider): an incident opens → the explanation is stored with its evidence list and confidence; viewing it 10 times makes 0 provider calls; provider outage → the job retries, then is marked failed, and the incident UI still works.
- Functional: the structured output (confidence, evidence) is validated, and malformed output from the model is rejected and retried once.

**DoD:** AI failure never affects incident handling.

#### M6.8 — Chat endpoint (SSE)
**Build:** `/ai/chat` runs the tool loop and streams events (token, tool_start, tool_result summary, done, error), with a conversation persisted per user for the session.

**Tests:**
- Integration (ScriptedProvider): the tool loop respects the limits; a client disconnect cancels the upstream provider call.
- Performance: the time to the first streamed event (a tool_start or token) is dominated by the provider, and JARVIS adds < 100 ms of overhead (measured with ScriptedProvider).

**DoD:** chat works end to end with the scripted provider, and in the nightly run with both real providers.

#### M6.9 — Recommendations and TTL simulation
**Spec:** §9.5
**Build:**
- The recommendation schema; `simulate_cache_policy` replaying `request_events` by `cache_key_hash`.
- Apply (creates a new config version linked to the recommendation) and dismiss (with a reason).
- A daily recommendation job.

**Tests:**
- Unit: the simulator against hand-computed fixtures (exact expected hit ratios).
- Validation: the simulator's prediction vs the actual measured hit ratio after applying a new TTL under S5. It must be within ±10 percentage points, and the result is logged as a benchmark.
- Functional: apply is admin-only; applying then rolling back restores the prior state; dismissed recommendations aren't regenerated for the same evidence.

**DoD:** simulator accuracy is logged and within tolerance.

#### M6.10 — AI settings, usage, and optional spend cap
**Spec:** §9.2
**Build:**
- Provider, model and key settings, plus a "Test connection" button.
- `ai_usage` tracking per call.
- The optional monthly token cap (off by default); when it's reached, AI features pause with a clear message.

**Tests:** functional: switching provider takes effect on the next job without a restart; reaching the cap pauses AI features, and gateway and incidents stay unaffected; the key is never returned by any endpoint.

**DoD:** runtime provider switching is proven.

#### M6.11 — AI UI
**Build (follow `web/DESIGN.md`):**
- **Incidents:** the explanation card on the incident page, with evidence chips that link to the charts they cite, a confidence badge, and a Regenerate button.
- **Ask JARVIS:** the page and a slide-over, with streaming text, collapsible tool-call cards, and suggested questions for the current page.
- **Recommendations:** a page with the diff and simulation result, plus Apply and Dismiss.
- **Settings:** an AI settings page.
- **AI off:** every AI surface collapses to a single "Configure AI" card.

**Tests:** E2E with the ScriptedProvider behind a test flag: a chat session with streaming; applying a recommendation; the AI-off state. Visual and accessibility baselines. Performance trace: streaming doesn't cause long tasks.

**DoD:** the complete AI experience works, and so does the AI-off experience.

#### M6.12 — AI evaluation suite
**Spec:** §9.8
**Build:**
- Fixtures from the P5 traces plus hand-built ambiguous cases, with expected categories and must-mention facts.
- A scoring runner.
- `ai-eval.yml` nightly, logging `ai_eval_score_*`, `ai_ttft_p50_*`, and `ai_tokens_per_explanation_*` to the gh-pages trend dashboard.

**Tests:** the runner itself is unit-tested on synthetic model outputs.

**DoD:** both providers meet the agreed thresholds: category accuracy ≥ 80%, zero false-certainty answers on the ambiguous fixtures. If a provider falls short, report it at the checkpoint rather than tuning prompts silently.

#### ✅ Checkpoint 6
**Acceptance stories:**
- *Given* no AI key, *then* the whole product works and AI surfaces show "Configure AI".
- *Given* a Claude key, *when* an incident opens, *then* an explanation with evidence appears without any user action; *when* I switch to Gemini in settings, *then* the next explanation uses Gemini with no restart.
- *Given* a recommendation with a simulation, *when* I apply it, *then* a new config version exists and the gateway follows within 1 s.
- *Given* the canary suite, *then* no secret or raw payload ever reaches a provider.

Include the eval scores for both providers in the report. Run the Part D protocol and stop.

---

### Phase 7 — Self-Host Polish

#### M7.1 — Setup, backup, and restore scripts
**Build:** `deploy/setup.sh` (generates `.env`, the master key and passwords; idempotent) and `scripts/backup.sh` / `scripts/restore.sh`.
**Tests:** system: setup → seed data → backup → destroy volumes → restore → the data is identical (a checksum query over key tables).
**DoD:** the round trip is proven in CI.

#### M7.2 — Release pipeline
**Build:** `release.yml`: multi-arch buildx (amd64, arm64) → GHCR; the compose file pinned to the release tag; a changelog generated from conventional commits; SBOMs attached (syft); images signed (cosign keyless).
**Tests:** a release dry run on a test tag; `docker compose pull` of that tag works on both architectures (arm64 via QEMU smoke test).
**DoD:** a published pre-release installs from images alone.

#### M7.3 — HTTPS and gateway exposure
**Build:** a Caddyfile with automatic HTTPS when `JARVIS_DOMAIN` is set (HTTP on localhost otherwise), and an optional gateway route with an IP allowlist.
**Tests:** system: a local CA test (Caddy's internal issuer) confirms TLS works; the gateway exposure route rejects IPs not on the allowlist.
**DoD:** documented in `docs/install.md`.

#### M7.4 — Documentation
**Build:** `README.md` (the quick start in ≤ 10 commands); `docs/install.md`, `upgrade.md`, `backup.md`, `configuration.md` (every setting); `integration-guide.md` (base-URL swap, key-slot trick, credential modes, with examples for 3 common REST APIs); `ai.md` (what data is sent, costs, switching providers); `architecture.md`; `troubleshooting.md`.
**Tests:** a docs link checker in CI; every command in the README is executed by the fresh-VM test (M7.5).
**DoD:** docs are reviewed by the user at the checkpoint.

#### M7.5 — Final acceptance and non-functional sign-off
**Tests:**
- **Fresh-VM acceptance:** a CI job on a clean runner follows only the README. Install → setup → onboard an API → traffic → an incident → an AI explanation (live provider, secrets-gated) → backup. Target: ≤ 10 minutes to a working install.
- **Upgrade test:** install the previous checkpoint's release, seed data, upgrade to the release candidate; the data survives and everything is healthy.
- **Footprint:** `stack_idle_rss_total_mb` ≤ 1.5 GB on the default profile.
- **Full non-functional run:** S1–S8, a 2 h soak, the chaos suite, the security suite, Lighthouse on every page. All results go into `benchmarks/checkpoints/P7.json`.
- **Full traceability:** 100% of requirements covered and passing.

**DoD:** everything green. Tag `v1.0.0` (not a pre-release).

#### ✅ Checkpoint 7 — Release
The final report must contain the full `BENCHMARKS.md` table across all phases, the traceability summary, the known limitations, and the future work in spec §19. Run the Part D protocol and stop.

---

## Appendix 1 — Frontend Questions (ask all at once before M4.7)

1. **Visual references:** Which 2–3 products' look do you like? (For example Linear, Vercel, Raycast, Grafana, Supabase, Stripe Dashboard, or anything else.) What do you specifically like about each?
2. **Mood:** calm and minimal, dense and "mission control", or playful? Should the "JARVIS" name influence the look (a subtle HUD accent) or stay neutral? The design must be original, and must not imitate film or franchise visuals.
3. **Theme:** dark by default, light by default, or follow the system setting?
4. **Brand color:** a specific accent color (hex), or should I propose options?
5. **Typography:** any preferred fonts? Should numbers and IDs use a monospace font?
6. **Density:** compact (more data per screen) or spacious (more breathing room)?
7. **Charts:** minimal lines, filled areas, or gradients? Should the incident window be shaded on charts?
8. **Motion:** none, subtle (fades and number transitions), or expressive?
9. **Logo:** do you have one, or should I design a simple original wordmark or mark?
10. **Screens and devices:** desktop only, or must it also work well on tablets and phones?
11. **Page priority:** which pages matter most to you for the first impression (e.g. Overview, Incidents, Ask JARVIS)?
12. **Existing assets:** any mockups, sketches, or screenshots to start from?
13. **Accessibility:** is WCAG 2.1 AA as the target acceptable?
14. **Bundle size:** is the default of 250 KB gzipped initial JS acceptable, or do you want it stricter?

After the user answers, follow the STOP steps in Phase 4 (visual directions → choice → `DESIGN.md`).

---

## Appendix 2 — Checkpoint Report Template

File: `docs/checkpoints/P<n>-<name>.md`. The same content is used as the GitHub pre-release notes.

```markdown
# Checkpoint P<n> — <Phase name>
Date · Commit SHA · Tag

## 1. In plain words
3–6 sentences a non-expert could follow: what JARVIS can do now that it couldn't before.

## 2. What was built
| Module | Summary | PR |
|--------|---------|----|

## 3. How it works now
Short walkthrough of the request path / data flow as of this phase,
highlighting what changed. One numbered-block diagram if helpful.

## 4. Tests
| Type | Added this phase | Total | Passing |
|------|------------------|-------|---------|
| Unit / Integration / System / Functional / E2E / Chaos / Security / Visual |
Coverage per package vs gates · Mutation score (if run) ·
Requirements covered: X/Y (link to traceability.md) ·
Skipped/xfail tests with issue links (must be empty or justified).

## 5. Acceptance stories
Each story → pass/fail → evidence (test name, screenshot, or log).

## 6. Benchmarks (numbers of record)
| Metric | Budget | This phase | Previous | Status |
Machine used (from env.json) · link to gh-pages trends · link to raw JSON.

## 7. Decisions and deviations
Anything that differs from SPEC.md, why, and the ADR link.

## 8. Known issues and tech debt
With issue links and severity.

## 9. Try it yourself
Exact commands to run the demo for this phase.

## 10. Next phase
What comes next, and any questions for the user.
```

---

## Appendix 3 — PR Template (`.github/pull_request_template.md`)

```markdown
## Module
M<x.y> — <name> · Spec: §… · Requirements: REQ-…

## What changed

## Tests added
- [ ] Unit  - [ ] Integration  - [ ] System/Functional  - [ ] E2E
- [ ] Chaos  - [ ] Security  - [ ] Visual  - [ ] Benchmark
Requirement IDs covered:

## Benchmark impact
Metrics touched and deltas (from CI job summary), or "none".

## Checklist
- [ ] No tests skipped/weakened  - [ ] Coverage gates met
- [ ] OpenAPI/telemetry snapshots updated if changed
- [ ] Docs updated  - [ ] PROGRESS.md updated
- [ ] New dependencies justified
```
