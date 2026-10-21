# JARVIS Progress

## Status

Phase 0 in progress. M0.1 complete and merged to `main`.

## Current Phase

Phase 0: Foundations

## Current Module

M0.2: Core package: models, interfaces, errors, requirements (implemented on `feat/m0.2-core-package`, PR pending)

## Completed

* `SPEC.md`, `IMPLEMENTATION.md`, `CLAUDE.md`, `SKILL.md`, `PROGRESS.md` created
* M0.1 Repository scaffold and tooling (PR #1, merge commit `3ef44a7`): uv workspace (core, gateway, api, worker, mock_upstream), root `pyproject.toml` (ruff, mypy strict on core/gateway, pytest markers, coverage), type-marker enforcement in `conftest.py` + meta test, pre-commit, Makefile (§B.2 targets; placeholders where suites don't exist), `.github/` templates + CODEOWNERS + minimal `ci.yml`, ADRs 0001–0013, README, `.gitignore`, `deploy/.env.example`.
  * Verified: local lint/typecheck/unit tests/coverage/pre-commit clean (7 tests passed); CI `python` job green on PR #1.

## In Progress

M0.2 on branch `feat/m0.2-core-package`: all build items and tests written; local lint, typecheck, 173 unit tests and coverage (core 99%) pass. Awaiting PR, CI and merge before marking complete.

## Next

Open the M0.2 PR, get CI green, and merge it. Then start M0.3 (compose skeleton and health) on `feat/m0.3-compose-health`.

## Blockers / Questions

None.

## Resume Point

Finish M0.2: open PR, wait for green CI, user merges, then record M0.2 complete. Do not begin M0.3 until then.

## Last Updated

2026-10-21
