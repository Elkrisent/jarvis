# JARVIS Progress

## Status

Phase 0 in progress. M0.1 implemented locally on branch `feat/m0.1-repo-scaffold`; minimal `ci.yml` added and branch pushed to origin; awaiting green CI on the PR.

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

M0.1 awaiting green CI on its PR.

## Next

Merge M0.1 after green CI, then M0.2 (core package: models, interfaces, errors, requirements).

## Blockers / Questions

* `gh` CLI is not installed, so the M0.1 PR must be opened in the browser (or install `gh`).
* `ci.yml` only triggers on PRs and pushes to `main`; M0.1 DoD "passes in CI" is confirmed once the PR's checks are green.

## Resume Point

Open the PR for `feat/m0.1-repo-scaffold`, confirm CI is green, squash-merge, then start M0.2. Do not begin M0.2 before that.

## Last Updated

2026-10-21
