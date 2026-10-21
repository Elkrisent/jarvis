# JARVIS Progress

## Status

Phase 0 in progress. M0.1 implemented locally on branch `feat/m0.1-repo-scaffold`; awaiting CI confirmation.

## Current Phase

Phase 0: Foundations

## Current Module

M0.1: Repository scaffold and tooling

## Completed

* `SPEC.md` created
* `IMPLEMENTATION.md` created
* `CLAUDE.md` created
* `SKILL.md` created
* `PROGRESS.md` created

* M0.1 (local): uv workspace (core, gateway, api, worker, mock_upstream), root `pyproject.toml` (ruff, mypy strict on core/gateway, pytest markers, coverage), type-marker enforcement plugin in `conftest.py` + meta test, pre-commit (ruff, mypy, gitleaks, EOF fixes), Makefile (all §B.2 targets, placeholders where suites don't exist), `.github/` PR/issue templates + CODEOWNERS, ADRs 0001 + D1–D12 (0002–0013), README, `.gitignore`, `deploy/.env.example`.
* Verified locally: `make lint`, `make typecheck`, `make test-unit` (7 passed), `make coverage`, `pre-commit run --all-files` all clean.

## In Progress

M0.1 awaiting DoD item "passes in CI" (no CI workflow or remote exists until M0.4).

## Next

Resolve the M0.1 CI question below, then M0.2 (core package: models, interfaces, errors, requirements).

## Blockers / Questions

* M0.1 DoD says `make test-unit` passes "locally and in CI", but CI workflows are M0.4 and the repo has no GitHub remote. Need user decision: accept local verification and move on, or add a minimal `ci.yml` now / push a remote first.
* Repo had no commits; M0.1 is committed on its feature branch only (no push, nothing on `main`).

## Resume Point

Finish M0.1 per the question above (open PR once a remote exists). Do not begin M0.2 until the user resolves it.

## Last Updated

2026-10-21
