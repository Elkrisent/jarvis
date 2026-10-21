# JARVIS Progress

## Status

Phase 0 (Foundations) complete: M0.1–M0.6 merged, Checkpoint 0 passed, pre-release `v0.0.0` published. Waiting for the user's approval to start Phase 1.

## Current Phase

Phase 0: Foundations (complete). Phase 1: Proxy (not started).

## Current Module

None active. Next is M1.1 (Phase 1), once approved.

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

Nothing.

## Next

1. User approves Phase 1 (apply any feedback on the checkpoint as a follow-up PR first).
2. Start M1.1 (config repositories and Alembic migrations) on `feat/m1.1-<slug>`; read IMPLEMENTATION.md M1.1 and the SPEC sections it cites first.
3. M1.1 is the first module with real Postgres code, so the `integration` job and its service containers start doing real work. Remember to check `make test-integration` still fails when a test fails.

## Blockers / Questions

None.

## Resume Point

Wait for the user's go-ahead for Phase 1. Then start M1.1 from `IMPLEMENTATION.md`. Do not begin M1.2 until the M1.1 Definition of Done is satisfied.

## Last Updated

2026-10-21
