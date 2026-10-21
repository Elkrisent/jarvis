# JARVIS Progress

## Status

Phase 0 in progress. M0.1–M0.6 complete (all Phase 0 modules) and merged to `main`.

## Current Phase

Phase 0: Foundations

## Current Module

Phase 0 modules done; Checkpoint 0 report awaiting approval

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

## In Progress

Checkpoint 0 on branch `chore/checkpoint-0`: all Part D steps 1-6 done and verified (317 unit, 9 integration, 7 system, 4 e2e, 1 chaos, 13 security tests pass; coverage gates met; `make bench-checkpoint PHASE=0` run; traceability gate for phase 0 passes with 30/246 covered; report in `docs/checkpoints/P0-foundations.md`). Step 7 (tag `v0.0.0` pre-release) happens after this PR is merged.

## Next

1. User merges the checkpoint PR.
2. Claude creates the `v0.0.0` pre-release on the merge commit (report as notes, `P0.json` attached).
3. Claude posts the summary and STOPS. User approves before Phase 1 (M1.1) starts; apply any feedback as a follow-up PR first.

## Blockers / Questions

None.

## Resume Point

Checkpoint 0 is done pending merge and the release tag. Do not begin Phase 1 (M1.1) until the user approves.

## Last Updated

2026-10-21
