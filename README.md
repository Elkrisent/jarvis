# JARVIS

API reliability gateway with retries, circuit breaking, caching, rate limiting,
telemetry and incident analysis. See [`docs/SPEC.md`](docs/SPEC.md) for what it
must do and [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) for the build order.

## Status

Phase 0 (Foundations) in progress. The repository scaffold, tooling and the `jarvis_core`
package (config models, errors, interfaces with in-memory implementations) exist;
the compose stack runs healthy but the gateway does not proxy anything yet. Current state: [`docs/PROGRESS.md`](docs/PROGRESS.md).

## Development setup

Required tools: Python 3.12, [`uv`](https://docs.astral.sh/uv/), `make`.

```bash
make sync           # install the uv workspace
make lint           # ruff check + format check
make typecheck      # mypy (strict on packages/core and services/gateway)
make test-unit      # unit tests
make coverage       # unit tests + per-package coverage gates
make traceability   # requirement -> test report in docs/traceability.md
make security-scan  # bandit + pip-audit
pre-commit install  # enable git hooks
```

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
scripts/              traceability generator
docs/                 spec, implementation guide, requirements, contributing, ADRs
```
