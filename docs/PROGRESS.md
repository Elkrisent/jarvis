# JARVIS Progress

## Status

Phase 0 in progress. M0.1 and M0.2 complete and merged to `main`.

## Current Phase

Phase 0: Foundations

## Current Module

M0.3: Compose skeleton and health (implemented on `feat/m0.3-compose-health`, PR pending)

## Completed

* `SPEC.md`, `IMPLEMENTATION.md`, `CLAUDE.md`, `SKILL.md`, `PROGRESS.md` created
* M0.1 Repository scaffold and tooling (PR #1, merge commit `3ef44a7`): uv workspace (core, gateway, api, worker, mock_upstream), root `pyproject.toml` (ruff, mypy strict on core/gateway, pytest markers, coverage), type-marker enforcement in `conftest.py` + meta test, pre-commit, Makefile (§B.2 targets; placeholders where suites don't exist), `.github/` templates + CODEOWNERS + minimal `ci.yml`, ADRs 0001–0013, README, `.gitignore`, `deploy/.env.example`.
  * Verified: local lint/typecheck/unit tests/coverage/pre-commit clean (7 tests passed); CI `python` job green on PR #1.
* M0.2 Core package (PR #3, merge commit `d251688`): `ApiDependency` Pydantic models with cross-field validation, `NormalizedRequest`/`AuthContext`, `JarvisError` hierarchy (`JV_*` codes with HTTP statuses), `Clock`/`Random` with fakes, eight core interfaces with in-memory implementations and reusable contract suites, `docs/requirements.md` (246 REQ-IDs), `scripts/traceability.py` + `make traceability`.
  * Verified: 173 unit tests pass, `jarvis_core` coverage 99%, lint/mypy/pre-commit clean; CI green on PR #3. 22 of 246 requirements have tests so far (gate not yet enforced).

## In Progress

M0.3 on branch `feat/m0.3-compose-health`: apps, Dockerfile, compose (default, dev, test override), `.env.example`, system/chaos/security tests written. Verified locally against a real Docker stack: 196 unit tests, 4 system, 1 chaos, 10 security tests pass; lint, mypy clean. Awaiting PR, CI and merge. CI does not run the stack tests until M0.4.

## Next

Open the M0.3 PR and merge it. Then start M0.4 (CI workflows) on `feat/m0.4-ci-workflows`; it must add the system and security workflows that run the stack tests.

## Blockers / Questions

None.

## Resume Point

Finish M0.3: PR, green CI, user merges, then record M0.3 complete. Do not begin M0.4 until then.

## Last Updated

2026-10-21
