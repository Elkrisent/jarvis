# JARVIS Progress

## Status

Phase 0 in progress. M0.1, M0.2 and M0.3 complete and merged to `main`.

## Current Phase

Phase 0: Foundations

## Current Module

M0.4: CI workflows (workflows written on `feat/m0.4-ci-workflows`, PR pending; gate proof outstanding)

## Completed

* `SPEC.md`, `IMPLEMENTATION.md`, `CLAUDE.md`, `SKILL.md`, `PROGRESS.md` created
* M0.1 Repository scaffold and tooling (PR #1, merge commit `3ef44a7`): uv workspace (core, gateway, api, worker, mock_upstream), root `pyproject.toml` (ruff, mypy strict on core/gateway, pytest markers, coverage), type-marker enforcement in `conftest.py` + meta test, pre-commit, Makefile (§B.2 targets; placeholders where suites don't exist), `.github/` templates + CODEOWNERS + minimal `ci.yml`, ADRs 0001–0013, README, `.gitignore`, `deploy/.env.example`.
  * Verified: local lint/typecheck/unit tests/coverage/pre-commit clean (7 tests passed); CI `python` job green on PR #1.
* M0.2 Core package (PR #3, merge commit `d251688`): `ApiDependency` Pydantic models with cross-field validation, `NormalizedRequest`/`AuthContext`, `JarvisError` hierarchy (`JV_*` codes with HTTP statuses), `Clock`/`Random` with fakes, eight core interfaces with in-memory implementations and reusable contract suites, `docs/requirements.md` (246 REQ-IDs), `scripts/traceability.py` + `make traceability`.
  * Verified: 173 unit tests pass, `jarvis_core` coverage 99%, lint/mypy/pre-commit clean; CI green on PR #3. 22 of 246 requirements have tests so far (gate not yet enforced).
* M0.3 Compose skeleton and health (PR #5, merge commit `c00280c`): FastAPI apps for gateway/api/worker with `/healthz` and `/readyz` (ok/degraded/unavailable, shared logic in `jarvis_core.health`), multi-stage non-root Dockerfile, `deploy/docker-compose.yml` + dev and test overrides, documented `.env.example`, system/chaos/security tests, Makefile targets wired.
  * Verified locally on a real Docker stack: 196 unit, 4 system, 1 chaos, 10 security tests pass; lint/mypy clean; CI `python` job green on PR #5. CI does not yet run the stack tests (M0.4).

## In Progress

M0.4 on `feat/m0.4-ci-workflows`: `ci.yml` (python + integration jobs, per-package coverage gates, job summary), `system.yml`, `security.yml` (bandit, pip-audit, gitleaks, trivy, stack security tests), `docs/contributing.md`. Local checks pass (207 unit, 4 system, 1 chaos, 10 security tests; pip-audit and trivy clean after upgrading vulnerable pins). Not done: workflows green on the PR and on `main`, branch protection applied by the user, and the deliberate failing-PR proof.

## Next

1. Merge the M0.4 PR once its checks are green, and confirm `system.yml` is green on `main`.
2. User applies branch protection per `docs/contributing.md`.
3. Open a deliberately failing PR to prove the gate blocks the merge, then close it.
4. Record M0.4 complete, then start M0.5 (mock upstream).

## Blockers / Questions

None.

## Resume Point

Finish M0.4: PR, green CI, branch protection, failing-PR proof. Do not begin M0.5 until the M0.4 Definition of Done is satisfied.

## Last Updated

2026-10-21
