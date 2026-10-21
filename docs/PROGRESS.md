# JARVIS Progress

## Status

Phase 0 in progress. M0.1 and M0.2 complete and merged to `main`.

## Current Phase

Phase 0: Foundations

## Current Module

M0.3: Compose skeleton and health (not started)

## Completed

* `SPEC.md`, `IMPLEMENTATION.md`, `CLAUDE.md`, `SKILL.md`, `PROGRESS.md` created
* M0.1 Repository scaffold and tooling (PR #1, merge commit `3ef44a7`): uv workspace (core, gateway, api, worker, mock_upstream), root `pyproject.toml` (ruff, mypy strict on core/gateway, pytest markers, coverage), type-marker enforcement in `conftest.py` + meta test, pre-commit, Makefile (§B.2 targets; placeholders where suites don't exist), `.github/` templates + CODEOWNERS + minimal `ci.yml`, ADRs 0001–0013, README, `.gitignore`, `deploy/.env.example`.
  * Verified: local lint/typecheck/unit tests/coverage/pre-commit clean (7 tests passed); CI `python` job green on PR #1.
* M0.2 Core package (PR #3, merge commit `d251688`): `ApiDependency` Pydantic models with cross-field validation, `NormalizedRequest`/`AuthContext`, `JarvisError` hierarchy (`JV_*` codes with HTTP statuses), `Clock`/`Random` with fakes, eight core interfaces with in-memory implementations and reusable contract suites, `docs/requirements.md` (246 REQ-IDs), `scripts/traceability.py` + `make traceability`.
  * Verified: 173 unit tests pass, `jarvis_core` coverage 99%, lint/mypy/pre-commit clean; CI green on PR #3. 22 of 246 requirements have tests so far (gate not yet enforced).

## In Progress

Nothing.

## Next

Start M0.3 on `feat/m0.3-compose-health` (see IMPLEMENTATION.md M0.3): minimal FastAPI apps for gateway, api and worker with `/healthz` and `/readyz`, multi-stage non-root Dockerfiles, `deploy/docker-compose.yml` (+ dev override and `test` profile), health checks, documented `.env.example`; system, chaos and security tests.

## Blockers / Questions

None.

## Resume Point

Start M0.3 from `IMPLEMENTATION.md`. Note: M0.3 adds FastAPI and Docker, so confirm pinned versions before adding dependencies. Do not begin M0.4 until the M0.3 Definition of Done is satisfied.

## Last Updated

2026-10-21
