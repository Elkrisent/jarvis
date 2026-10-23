# JARVIS

API reliability gateway with retries, circuit breaking, caching, rate limiting,
telemetry and incident analysis. See [`docs/SPEC.md`](docs/SPEC.md) for what it
must do and [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) for the build order.

## Status

Phase 1 (Proxy) in progress. The repository scaffold, tooling, CI, benchmark infrastructure and the
`jarvis_core` package (config models, errors, interfaces with in-memory implementations) exist, and
the `api` service now owns the Postgres schema: Alembic migrations run at startup under an advisory
lock, and `PostgresConfigSource` stores API policies as immutable versions. M1.2 added the AES-256-GCM `SecretBox`,
the Postgres secret store and the `jarvis rotate-master-key` command; M1.3 added the `/api/v1` management API (projects, JARVIS keys, API dependencies; the api now
requires `JARVIS_MASTER_KEY` and `JARVIS_ADMIN_TOKEN` at startup). The compose stack runs
healthy. M1.5 added gateway authentication: `/gw/{api_slug}/...` checks the JARVIS key (header or credential slot, project scope, allowed APIs, revocation within 1 s), M1.6 added route templating and M1.7 the SSRF guard: saving an API with a private, loopback, link-local or plain-http upstream is refused unless it is in `JARVIS_SSRF_ALLOWLIST`. M1.8 made the gateway forward: it sends the request to the API's `base_url` with the JARVIS key removed and the real provider credential injected (`bearer`, `header` or `query`), over a transport that validates and pins every connection, and returns the upstream's answer unchanged. It makes one attempt per request: retries, caching and circuit breaking come in later modules. The gateway now needs `JARVIS_MASTER_KEY` too, to open provider secrets into memory. Start with [`docs/quickstart-dev.md`](docs/quickstart-dev.md). Current state: [`docs/PROGRESS.md`](docs/PROGRESS.md).

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
make bench-propagation   # config propagation p99 (needs DATABASE_URL and REDIS_URL)
make bench-up bench-load bench-down   # k6 dry run against the mock upstream (needs Docker)
make bench-checkpoint PHASE=0         # numbers of record -> BENCHMARKS.md (local machine)
pre-commit install  # enable git hooks
```

Operator command (from the `api` service): `jarvis rotate-master-key` re-encrypts every stored
secret from `JARVIS_MASTER_KEY` to `JARVIS_NEW_MASTER_KEY`, atomically. See
[`docs/secrets-rotation.md`](docs/secrets-rotation.md) for the runbook.

### Run the stack

```bash
cp deploy/.env.example deploy/.env      # then set POSTGRES_PASSWORD, JARVIS_MASTER_KEY, JARVIS_ADMIN_TOKEN (docs/quickstart-dev.md)
cd deploy && docker compose up -d --build --wait
```

Gateway, api and worker each serve `/healthz` (liveness) and `/readyz` (dependencies) on
internal port 8080 and publish no host ports. For development use
`-f docker-compose.yml -f docker-compose.dev.yml` (hot reload, gateway on 127.0.0.1:8090, api on 8081, worker on 8082).
The gateway loads every API policy and key hash from Postgres at startup (`DATABASE_URL`), keeps
them current through Redis events (within 1 s) and a full resync every `JARVIS_RESYNC_INTERVAL_S`
(default 60), and writes the last loaded config to `JARVIS_SNAPSHOT_PATH` (the `gateway-snapshot`
volume) so it can start from that file while Postgres is down. Compose starts the gateway only after Postgres is healthy, so with Postgres down a plain `docker compose up` will not start it; snapshot starts work through the restart policy, `docker compose restart gateway` or `docker start`. A gateway started from the file serves keys revoked while it was down until Postgres returns (revocation within 1 s does not hold in that mode), so a file synced more than `JARVIS_SNAPSHOT_MAX_AGE_S` ago (default 24 h) is ignored and the gateway stays not ready. Its `/readyz` adds `config`
(`source`, `apis`, `keys`, `synced_at`; counts only): `degraded` when serving the snapshot file or
when no full sync succeeded for 3 intervals, `unavailable` (503) when no config is loaded at all.
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
