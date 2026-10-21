# JARVIS Progress

## Status

Phase 0 in progress. M0.1 complete and merged to `main`.

## Current Phase

Phase 0: Foundations

## Current Module

M0.2: Core package: models, interfaces, errors, requirements (not started)

## Completed

* `SPEC.md`, `IMPLEMENTATION.md`, `CLAUDE.md`, `SKILL.md`, `PROGRESS.md` created
* M0.1 Repository scaffold and tooling (PR #1, merge commit `3ef44a7`): uv workspace (core, gateway, api, worker, mock_upstream), root `pyproject.toml` (ruff, mypy strict on core/gateway, pytest markers, coverage), type-marker enforcement in `conftest.py` + meta test, pre-commit, Makefile (§B.2 targets; placeholders where suites don't exist), `.github/` templates + CODEOWNERS + minimal `ci.yml`, ADRs 0001–0013, README, `.gitignore`, `deploy/.env.example`.
  * Verified: local lint/typecheck/unit tests/coverage/pre-commit clean (7 tests passed); CI `python` job green on PR #1.

## In Progress

Nothing.

## Next

Start M0.2 on branch `feat/m0.2-core-package` (see IMPLEMENTATION.md M0.2): Pydantic `ApiDependency` models with cross-field validation, request object and `AuthContext`, core interfaces with in-memory implementations (including `Clock`/`Random`), `JarvisError` hierarchy, `docs/requirements.md` with REQ-IDs, `scripts/traceability.py`. Write tests first for logic.

## Blockers / Questions

None.

## Resume Point

Start M0.2 from `IMPLEMENTATION.md`. Do not begin M0.3 until the M0.2 Definition of Done is satisfied.

## Last Updated

2026-10-21
