# JARVIS Progress

## Status

Phase 0 (Foundations) complete (Checkpoint 0 passed, `v0.0.0`). Phase 1 started with the user's approval: M1.1 is implemented on `feat/m1.1-db-schema-migrations` and awaiting PR review and merge.

## Current Phase

Phase 1: Proxy.

## Current Module

M1.1 Database schema and migrations (implemented, not yet merged).

## Completed

* `SPEC.md`, `IMPLEMENTATION.md`, `CLAUDE.md`, `SKILL.md`, `PROGRESS.md` created
* M0.1 Repository scaffold and tooling (PR #1, merge commit `3ef44a7`): uv workspace (core, gateway, api, worker, mock_upstream), root `pyproject.toml` (ruff, mypy strict on core/gateway, pytest markers, coverage), type-marker enforcement in `conftest.py` + meta test, pre-commit, Makefile (§B.2 targets; placeholders where suites don't exist), `.github/` templates + CODEOWNERS + minimal `ci.yml`, ADRs 0001–0013, README, `.gitignore`, `deploy/.env.example`.
  * Verified: local lint/typecheck/unit tests/coverage/pre-commit clean (7 tests passed); CI `python` job green on PR #1.
* M0.2 Core package (PR #3, merge commit `d251688`): `ApiDependency` Pydantic models with cross-field validation, `NormalizedRequest`/`AuthContext`, `JarvisError` hierarchy (`JV_*` codes with HTTP statuses), `Clock`/`Random` with fakes, eight core interfaces with in-memory implementations and reusable contract suites, `docs/requirements.md` (246 REQ-IDs), `scripts/traceability.py` + `make traceability`.
  * Verified: 173 unit tests pass, `jarvis_core` coverage 99%, lint/mypy/pre-commit clean; CI green on PR #3. 22 of 246 requirements have tests so far (gate not yet enforced).
* M0.3 Compose skeleton and health (PR #5, merge commit `c00280c`): FastAPI apps for gateway/api/worker with `/healthz` and `/readyz` (ok/degraded/unavailable, shared logic in `jarvis_core.health`), multi-stage non-root Dockerfile, `deploy/docker-compose.yml` + dev and test overrides, documented `.env.example`, system/chaos/security tests, Makefile targets wired.
  * Verified locally on a real Docker stack: 196 unit, 4 system, 1 chaos, 10 security tests pass; lint/mypy clean; CI `python` job green on PR #5. CI does not yet run the stack tests (M0.4).
* M0.4 CI workflows (PR #7, merge commit `1816e0b`): `ci.yml` (python + integration jobs, per-package coverage gates with 1-point drop rule), `system.yml` (label/main), `security.yml` (bandit, pip-audit, gitleaks, trivy x3, stack security tests), uv and Docker layer caching, `docs/contributing.md`. Pins upgraded to clear pip-audit/trivy findings (fastapi 0.156.5, starlette 1.9.0, pytest 9.1.1, pytest-asyncio 1.4.0, python 3.12.15 base).
  * Verified: all workflows green on PR #7 and on `main`. Repo ruleset `jarvis` (applied by the user) requires 9 checks. Gate proven: throwaway PR #8 with a failing test was `BLOCKED` (not mergeable even as admin) and closed unmerged.
* M0.5 Mock upstream (PR #10, merge commit `76384b8`): `tools/mock_upstream` rule-driven catch-all and admin API (rules, call log, reset, seeded mode), README, 60 unit + 9 integration tests, `test`-profile compose service, `mock_upstream` fixture, stack tests.
  * Verified: all checks green on PR #10 (including `system` and `integration`).
* M0.6 Benchmark infrastructure (PR #11, merge commit `4763024`): `benchmarks/budgets.yaml` (SPEC §13), pytest-benchmark micro-benchmark, k6 config + dry run, `scripts/bench/` tooling (k6 converter, overhead, budget checker, env.json, docker stats sampler, checkpoint writer), `make bench-*`, `bench.yml`, `BENCHMARKS.md`, `benchmarks/checkpoints/P0.json`.
  * Verified: 62 script tests; `make bench-checkpoint PHASE=0` run locally end to end; `bench.yml` green on `main` (run 39575593691); `gh-pages` created and Pages enabled by the user (https://elkrisent.github.io/jarvis/); `bench/micro`, `bench/load` and `bench/load-throughput` serve data points (e.g. mock_direct_p50 3.3 ms, 2,899 req/s on the hosted runner).
* Checkpoint 0 (PR #13, merge commit `0ef369e`, tag `v0.0.0` pre-release with `P0.json` attached): report in `docs/checkpoints/P0-foundations.md`. All three acceptance stories automated in `tests/e2e/test_checkpoint_0.py`; `make traceability COMPLETED_PHASE=0 TRACE_MARKERS="not perf"` passes (30/246 requirements covered, all `pass`); budgets in scope met (idle footprint 210 MB vs 1536 MB).
  * Verified: 317 unit, 9 integration, 7 system, 4 e2e, 1 chaos, 13 security tests; `ci`, `system`, `security` and `bench` all green on `0ef369e`. The user also ran the report's "try it yourself" commands on their machine: all passed.

## In Progress

M1.1 Database schema and migrations, branch `feat/m1.1-db-schema-migrations` (not yet pushed or merged):

* `jarvis_api.db`: SQLAlchemy 2.0 models and a hand-written reversible Alembic migration `0001` for `users`, `projects`, `jarvis_keys`, `api_dependencies`, `api_policy_versions`, `secrets`, `settings`, `audit_log`; `run_migrations` under a Postgres advisory lock, called from the api lifespan (a failed migration stops startup; `DATABASE_URL` `sslmode` is translated for asyncpg; a database ahead of this code is left alone; each step is logged, including waiting for the lock); `PostgresConfigSource` behind `ConfigSource` (text prefixed IDs; every `put` is a new immutable policy version).
* Pinned new deps in `services/api`: sqlalchemy 2.1.6, alembic 1.20.1, asyncpg 0.32.0. `.gitignore` now ignores `.claude/worktrees/`.
* Coverage now counts unit + integration tests (user-approved, thresholds unchanged): the `coverage` step moved from the `python` job to the `integration` job in `ci.yml`, `make coverage` runs `-m "unit or integration"`, and the meta test and `docs/contributing.md` were updated to match.
* Verified locally against Postgres 16: 373 unit + integration tests pass; ruff, mypy, bandit and pip-audit clean; `services/api` coverage 100% line / 100% branch against 85% / 75% gates, no drop vs `main` (checked locally with a `main` baseline); migrations upgrade, downgrade and upgrade again; two and five concurrent startups apply migrations exactly once (the lock test fails without the lock); a model-vs-migration parity test passes; `make test-integration` exits non-zero when tests fail.
* Verified with Docker: all 399 non-perf tests pass (unit, integration, system, e2e, chaos, security), including the new `tests/system/test_api_migrations.py`, which shows the compose `api` migrates the stack's Postgres at startup. `docs/traceability.md` regenerated (31/246 covered; REQ-12-2 now linked, Checkpoint 0 rows still `pass`).
* Not yet verified: the CI run itself.

## Next

1. Push the branch, open the M1.1 PR, and get `ci`, `system` and `security` green (watch the `integration` job: it now runs the coverage gates).
2. After merge, record the merge SHA here (separate docs PR, as for earlier modules).
3. Then start M1.2 SecretBox (IMPLEMENTATION.md M1.2, SPEC §11.2); do not begin it until M1.1's Definition of Done is confirmed by CI.

## Blockers / Questions

None.

## Resume Point

Open and shepherd the M1.1 PR from `feat/m1.1-db-schema-migrations`. Once it is merged and recorded, start M1.2.

## Last Updated

2026-10-22
