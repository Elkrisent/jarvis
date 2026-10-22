# JARVIS

API reliability gateway with retries, circuit breaking, caching, rate limiting,
telemetry and incident analysis. See [`docs/SPEC.md`](docs/SPEC.md) for what it
must do and [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) for the build order.

## Status

Phase 1 (Proxy) in progress. The repository scaffold, tooling, CI, benchmark infrastructure and the
`jarvis_core` package (config models, errors, interfaces with in-memory implementations) exist, and
the `api` service now owns the Postgres schema: Alembic migrations run at startup under an advisory
lock, and `PostgresConfigSource` stores API policies as immutable versions. M1.2 (AES-256-GCM `SecretBox`,
Postgres secret store and the `jarvis rotate-master-key` command) is implemented on its branch and
awaiting merge; the api does not use the master key at startup yet. The compose stack runs
healthy but the gateway does not proxy anything yet. Current state: [`docs/PROGRESS.md`](docs/PROGRESS.md).

## Development setup

Required tools: Python 3.12, [`uv`](https://docs.astral.sh/uv/), `make`.

```bash
make sync           # install the uv workspace
make lint           # ruff check + format check
make typecheck      # mypy (strict on packages/core and services/gateway)
make test-unit      # unit tests
make test-integration  # needs Postgres and Redis: set DATABASE_URL, REDIS_URL (see docs/contributing.md)
make coverage       # unit + integration tests + per-package coverage gates (same requirements)
make traceability   # requirement -> test report in docs/traceability.md
make security-scan  # bandit + pip-audit
make bench-micro    # pytest-benchmark micro-benchmarks
make bench-up bench-load bench-down   # k6 dry run against the mock upstream (needs Docker)
make bench-checkpoint PHASE=0         # numbers of record -> BENCHMARKS.md (local machine)
pre-commit install  # enable git hooks
```

Operator command (from the `api` service): `jarvis rotate-master-key` re-encrypts every stored
secret from `JARVIS_MASTER_KEY` to `JARVIS_NEW_MASTER_KEY`, atomically. See
[`docs/secrets-rotation.md`](docs/secrets-rotation.md) for the runbook.

### Run the stack

```bash
cp deploy/.env.example deploy/.env      # then set POSTGRES_PASSWORD
cd deploy && docker compose up -d --build --wait
```

Gateway, api and worker each serve `/healthz` (liveness) and `/readyz` (dependencies) on
internal port 8080 and publish no host ports. For development use
`-f docker-compose.yml -f docker-compose.dev.yml` (hot reload, ports on 127.0.0.1:8080-8082).
`make test-system`, `make test-chaos` and `make test-security` start their own stack
(project `jarvis-test`, ports 18080-18082, mock upstream on 19000 via the `test` profile) and need Docker.

Other `make test-*` / `make bench-*` targets are placeholders until their suites land.

## Layout

```text
packages/core/        shared models, interfaces, errors
services/gateway/     data plane
services/api/         control plane
services/worker/      telemetry, analytics, AI
tools/mock_upstream/  controllable mock upstream (test asset, see its README)
web/                  React app (not started)
deploy/               compose files (default, dev, test), Dockerfile, env example
tests/                meta, e2e, load, ai_eval
scripts/              traceability, coverage gate, benchmark tooling (scripts/bench)
benchmarks/           budgets.yaml (from SPEC §13) and per-phase checkpoint snapshots
tests/load/           k6 scripts and shared config
docs/                 spec, implementation guide, requirements, contributing, ADRs
```
