# JARVIS

API reliability gateway with retries, circuit breaking, caching, rate limiting,
telemetry and incident analysis. See [`docs/SPEC.md`](docs/SPEC.md) for what it
must do and [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) for the build order.

## Status

Phase 0 (Foundations) in progress. The repository scaffold, tooling and the `jarvis_core`
package (config models, errors, interfaces with in-memory implementations) exist;
there is no runnable gateway yet. Current state: [`docs/PROGRESS.md`](docs/PROGRESS.md).

## Development setup

Required tools: Python 3.12, [`uv`](https://docs.astral.sh/uv/), `make`.

```bash
make sync           # install the uv workspace
make lint           # ruff check + format check
make typecheck      # mypy (strict on packages/core and services/gateway)
make test-unit      # unit tests
make coverage       # unit tests with coverage
make traceability   # requirement -> test report in docs/traceability.md
pre-commit install  # enable git hooks
```

Other `make test-*` / `make bench-*` targets are placeholders until their suites land.

## Layout

```text
packages/core/        shared models, interfaces, errors
services/gateway/     data plane
services/api/         control plane
services/worker/      telemetry, analytics, AI
tools/mock_upstream/  controllable mock upstream (test asset)
web/                  React app (not started)
deploy/               compose files, env example (not started)
tests/                meta, e2e, load, ai_eval
scripts/              traceability generator
docs/                 spec, implementation guide, requirements, ADRs
```
