# JARVIS Progress

## Status

Phase 0 (Foundations) complete (Checkpoint 0 passed, `v0.0.0`). Phase 1 in progress: M1.1 merged (PR #15, merge commit `828922e`); M1.2 merged (PR #16, merge commit `7b9e3c6`); M1.3 merged (PR #18, merge commit `7d8acec`); M1.4 implemented and verified on `feat/m1.4-config-loader` (PR not yet opened).

## Current Phase

Phase 1: Proxy.

## Current Module

M1.4 Gateway config loader: implemented on `feat/m1.4-config-loader`, awaiting final review and PR.

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

* M1.1 Database schema and migrations (PR #15, merge commit `828922e`):
  * `jarvis_api.db`: SQLAlchemy 2.0 models and a hand-written reversible Alembic migration `0001` for `users`, `projects`, `jarvis_keys`, `api_dependencies`, `api_policy_versions`, `secrets`, `settings`, `audit_log`; `run_migrations` under a Postgres advisory lock, called from the api lifespan (a failed migration stops startup; `DATABASE_URL` `sslmode` is translated for asyncpg; a database ahead of this code is left alone; each step is logged, including waiting for the lock); `PostgresConfigSource` behind `ConfigSource` (text prefixed IDs; every `put` is a new immutable policy version).
  * Pinned new deps in `services/api`: sqlalchemy 2.1.6, alembic 1.20.1, asyncpg 0.32.0. `.gitignore` now ignores `.claude/worktrees/`.
  * Coverage now counts unit + integration tests (user-approved, thresholds unchanged): the `coverage` step moved from the `python` job to the `integration` job in `ci.yml`, `make coverage` runs `-m "unit or integration"`, and the meta test and `docs/contributing.md` were updated to match.
  * Verified locally against Postgres 16: 373 unit + integration tests pass; ruff, mypy, bandit and pip-audit clean; `services/api` coverage 100% line / 100% branch against 85% / 75% gates, no drop vs `main` (checked locally with a `main` baseline); migrations upgrade, downgrade and upgrade again; two and five concurrent startups apply migrations exactly once (the lock test fails without the lock); a model-vs-migration parity test passes; `make test-integration` exits non-zero when tests fail.
  * Verified with Docker: all 399 non-perf tests pass (unit, integration, system, e2e, chaos, security), including the new `tests/system/test_api_migrations.py`, which shows the compose `api` migrates the stack's Postgres at startup. CI for PR #15 was green before merge.
* M1.2 SecretBox (PR #16, merge commit `7b9e3c6`):
  * `jarvis_core.secretbox`: `AesGcmSecretBox` (AES-256-GCM, random 96-bit nonce per record, key ID `k_<16 hex of sha256(key)>` stored with the record and authenticated as associated data), strict `parse_master_key` (32 bytes, standard base64, whitespace tolerated, errors never echo the input), `secret_box_from_env` for `JARVIS_MASTER_KEY`. New pinned dep `cryptography==50.0.2`.
  * `jarvis_api.db.secrets.PostgresSecretStore` and `jarvis rotate-master-key` (`jarvis_api.cli`): one transaction under `LOCK TABLE secrets`, every row validated before any is rewritten, all-or-nothing; keys come only from `JARVIS_MASTER_KEY` / `JARVIS_NEW_MASTER_KEY`, output shows key IDs and counts only; exit codes 0/1/2.
  * Docs and config: `docs/secrets-rotation.md` (runbook), `deploy/.env.example`, README, `docs/contributing.md`, `docs/traceability.md` (35/246 covered; REQ-11.2-1 to -4 linked).
  * Not wired yet: the api does not load the master key at startup (planned for M1.3, where secrets are first used).
  * Code-review fixes (all applied, each with a regression test): the CLI no longer reports every database error as a connection failure; each record is bound to its row id as associated data (a ciphertext copied to another row fails authentication; `AesGcmSecretBox.seal/open` take an optional `context`, the `SecretBox` protocol is unchanged; `PostgresSecretStore.add(box, plaintext)` / `reveal(box, id)`); key ID widened to 64 bits (`k_` + 16 hex); `to_async_url` maps `connect_timeout` and rejects other libpq-only options by name instead of failing at connect time (M1.1 code); `run_migrations` cleanup no longer masks the real error; `create_app_from_env` reports a missing `DATABASE_URL` clearly; the CI `integration` job runs the integration tests once (via `make coverage`) instead of twice plus a baseline.
  * Verified locally against Postgres 16 and Redis 7: ruff, mypy, bandit and pip-audit clean; 438 unit + integration tests pass under `make coverage` (core 100% line / 99% branch, api 100% / 100%, gates met); 468 non-perf tests pass in the traceability run; Checkpoint 0 rows still `pass`. Regression tests were checked to fail without their fix (migration cleanup).
  * `ci` and `security` checks were green on PR #16 before merge; `system` is skipped on PRs without the label (check it on `main`).
* M1.3 Minimal management API (PR #18, merge commit `7d8acec`):
  * `/api/v1` projects, JARVIS keys (`jv_live_` + base62 of 32 random bytes, only the SHA-256 hash stored, shown once) and API dependencies (versioned; provider secrets sealed with the master key, `has_secret` only on output); admin bearer token auth; 422 errors with field paths; `config:changed` published to Redis on API create/update; `/openapi.json` behind the token with a committed snapshot (`make openapi-snapshot`).
  * The api refuses to start without `JARVIS_MASTER_KEY` and `JARVIS_ADMIN_TOKEN`. New pinned dependency `redis==8.0.1`. Compose, `deploy/.env.example`, test override (placeholder secrets), README, `docs/contributing.md` and `docs/quickstart-dev.md` updated.
  * Verified locally against Postgres 16 and Redis: `make lint typecheck`, `make security-scan` clean; 641 unit + integration tests pass under `make coverage` (core 100/99, api 100/100, gates met; no `main` baseline available locally). `docs/traceability.md` regenerated (41/246 covered; REQ-10-1, -10-2, -5.2-1, -11.2-4 list tests).
  * Verified on a Docker host (Docker 28.5, Compose 2.40): `make test-system` 10 passed (including `tests/system/test_management_api.py`), `make test-e2e` 4, `make test-chaos` 1, `make test-security` 17; `make traceability COMPLETED_PHASE=0 TRACE_MARKERS="not perf"` 672 passed, Checkpoint 0 rows `pass`, no `not run` rows. Every command in `docs/quickstart-dev.md` was run against a real dev stack and behaved as documented (the gateway container could not bind host port 8080 on this machine, so only the api was used; the quickstart needs only the api).
  * gitleaks (run through its Docker image over `main..HEAD`) flagged the placeholder master key in `deploy/docker-compose.test.yml`; a narrow `.gitleaks.toml` allowlist (that value in that file only; a different key in the same file is still caught) clears it.
  * `ci` and `security` were green on PR #18 (a first `integration` run failed the no-drop-vs-main coverage gate; fixed with a test, api back to 100/100). `ci`, `security`, `system` and `bench` are all green on `main` at `7d8acec`.
* M1.4 Gateway config loader (branch `feat/m1.4-config-loader`, PR not yet opened):
  * `jarvis_gateway.config`: immutable `Snapshot` with atomic swap in `ConfigStore`; `PostgresLoader` (read-only, repeatable-read, tells an invalid API row from a gone one); on-disk snapshot file (written after every load, coalesced) as the fallback when Postgres is down; `ConfigManager` subscribes to `config:changed` and the new `config:invalidated` (published by the api on key create/revoke, API delete, project delete), resyncs every 60 s and at startup, resyncs early after a failed refresh. The gateway loads config at startup and reports it (counts only) in `/readyz`; unreadable config makes `/readyz` 503 instead of crashing.
  * Revocation push (REQ-11.1-6) is now implemented through `config:invalidated`; M1.5 enforces it.
  * Benchmark: `services/gateway/tests/bench/test_config_propagation.py` (`perf`), `make bench-propagation`, and a `propagation` job in `bench.yml`. Local (not CI) run, 200 PATCHes, measured from before the request is sent: p50 10.8 ms, p99 20.8 ms vs 1000 ms budget (details in `BENCHMARKS.md`). The test is tagged REQ-13-5 and REQ-5.2-3; the system test keeps its REQ-13-5 tag as a single-sample check.
  * Verified locally against Postgres and Redis: `make lint typecheck` clean; 233 gateway unit + integration tests pass; stack suites system, chaos, e2e and security passed earlier on this branch (gateway startup/readiness work); 913 passed (2 perf deselected) in the final `make traceability COMPLETED_PHASE=0 TRACE_MARKERS="not perf"` run (47/246 covered; REQ-13-5 and REQ-5.2-3 rows changed and regenerated); `make bench-propagation` passed 3 times.
  * NOT run: the GitHub `ci`, `security`, `system` and `bench` workflows (including the new `propagation` job, validated only as YAML); the dedicated least-privilege role path of the Postgres loader test (the fallback path ran locally; CI runs it).


## In Progress

M1.4 is complete on the branch; final review is pending.

## Next

1. Final review of the M1.4 branch, then open the M1.4 PR and get `ci` and `security` green (and `system`, `bench` on main after merge); record the merge SHA here in a docs PR.
2. Then M1.5 Gateway authentication.
3. Before the P1 checkpoint: wire `make bench-propagation` (needs `DATABASE_URL`/`REDIS_URL`) into `scripts/bench/run_checkpoint.sh`. Today the checkpoint skips it, so `config_propagation_p99` stays "not measured" in the `BENCHMARKS.md` table and the interim section there is dropped by the next `make bench-checkpoint`.

## Blockers / Questions

* OPEN, needs a user decision before P5: PATCH deletes the old secret, but older immutable versions still hold its `secret_ref`. A P5 rollback (copy an old version, SPEC section 5.2) would point at a deleted secret. M1.4 reads only the current version.
* REQ-11.1-6: the revocation push is implemented in M1.4 (`config:invalidated`); the traceability row only becomes meaningful once M1.5 enforces it on requests.
* For M1.5:
  * Do not trust a `(project, slug)` lookup blindly: a transient duplicate slug can exist in the snapshot until the next resync (up to 60 s if an event load fails).
  * A key's `allowed_api_ids` can name deleted APIs; deny on an unknown ID, never treat an empty or unknown list as unrestricted.

## Resume Point

Final review of `feat/m1.4-config-loader`, then open the PR; M1.5 starts after the merge.

## Last Updated

2026-10-23
