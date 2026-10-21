# JARVIS Progress

## Status

Phase 0 in progress. M0.1–M0.5 complete and merged to `main`.

## Current Phase

Phase 0: Foundations

## Current Module

M0.6: Benchmark infrastructure (implemented on `feat/m0.6-bench-infra`, PR pending)

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

## In Progress

M0.6 on `feat/m0.6-bench-infra`: `benchmarks/budgets.yaml`, pytest-benchmark micro-benchmark, k6 config and dry-run scenario, scripts in `scripts/bench/` (k6 converter, overhead, budget checker, env.json, docker stats sampler, checkpoint writer), `make bench-*` targets, `bench.yml`, `BENCHMARKS.md` and `benchmarks/checkpoints/P0.json` from a local run. Verified locally: `make bench-checkpoint PHASE=0` end to end, 62 script tests. Not done: `bench.yml` first run on `main`, `gh-pages` branch created, GitHub Pages enabled by the user, a data point visible on the chart (the DoD).

## Next

1. Merge the M0.6 PR; `bench.yml` runs on `main` and creates `gh-pages`.
2. User enables Pages (Settings, Pages, Deploy from branch `gh-pages`, root).
3. Confirm the chart page shows at least one data point, then record M0.6 complete.
4. Then run the Checkpoint 0 protocol (IMPLEMENTATION Part D) and stop for approval.

## Blockers / Questions

None.

## Resume Point

Finish M0.6: merge, `bench.yml` run, Pages enabled, chart data point confirmed. Then the Checkpoint 0 protocol. Do not begin Phase 1 without approval.

## Last Updated

2026-10-21
