.PHONY: help sync lint format typecheck security-scan bandit pip-audit \
	test-unit test-integration test-system test-functional test-e2e test-chaos \
	test-security test-web test-all bench-micro bench-load bench-soak \
	bench-checkpoint bench-up bench-down traceability coverage

PYTEST = uv run pytest
BENCH_DIR = build/bench
COMPOSE_TEST = docker compose -p jarvis-test --profile test -f deploy/docker-compose.yml \
	-f deploy/docker-compose.test.yml --env-file deploy/.env.example

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1 | tr '\n' ' '; echo

sync:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

security-scan: bandit pip-audit

bandit:
	uv run bandit -q -r packages/core/src services/gateway/src services/api/src services/worker/src tools -ll

pip-audit:
	mkdir -p build
	uv export --locked --no-emit-workspace --no-hashes --format requirements-txt > build/requirements-audit.txt
	uv run pip-audit -r build/requirements-audit.txt --no-deps --disable-pip

test-unit:
	$(PYTEST) -m unit

test-integration:
	$(PYTEST) -m integration

test-system:
	$(PYTEST) -m system

test-functional:
	@echo "placeholder: no functional suites yet (arrives with M0.2)"

test-e2e:
	@echo "placeholder: no e2e suites yet"

test-chaos:
	$(PYTEST) -m chaos

test-security:
	$(PYTEST) -m security

test-web:
	@echo "placeholder: web/ arrives in M4.7"

test-all: test-unit test-integration test-system test-functional test-e2e test-web

bench-micro:
	mkdir -p $(BENCH_DIR)
	$(PYTEST) -m perf packages/core/tests/bench --benchmark-json=$(BENCH_DIR)/micro.json -q
	uv run python scripts/bench/micro_results.py $(BENCH_DIR)/micro.json --results $(BENCH_DIR)/results.json

# Needs the mock upstream running: `make bench-up`.
bench-load:
	mkdir -p $(BENCH_DIR)/k6
	scripts/bench/run_k6.sh tests/load/scenarios/dry_run.js $(BENCH_DIR)/k6/dry_run.json
	uv run python scripts/bench/k6_to_gab.py $(BENCH_DIR)/k6/dry_run.json --prefix mock_direct \
		--smaller $(BENCH_DIR)/load-smaller.json --bigger $(BENCH_DIR)/load-bigger.json \
		--results $(BENCH_DIR)/results.json

bench-up:
	$(COMPOSE_TEST) up -d --build --wait mock-upstream

bench-down:
	$(COMPOSE_TEST) down -v --remove-orphans

bench-soak:
	@echo "placeholder: benchmark infrastructure arrives in M0.6"

# Numbers of record, run locally on a documented machine: make bench-checkpoint PHASE=0
bench-checkpoint:
	@test -n "$(PHASE)" || { echo "usage: make bench-checkpoint PHASE=<0-7>"; exit 2; }
	scripts/bench/run_checkpoint.sh $(PHASE)

# Set COMPLETED_PHASE=<n> (done at each checkpoint) to fail on uncovered requirements of phases <= n.
COMPLETED_PHASE ?=

traceability:
	$(PYTEST) -m unit -q --junit-xml=build/junit.xml
	uv run python scripts/traceability.py --junit build/junit.xml \
		$(if $(COMPLETED_PHASE),--completed-phase $(COMPLETED_PHASE))

# Unit-test coverage, then the per-package gates (B.3). Pass BASELINE=<coverage.json from main> to
# also fail on a drop of more than 1 point.
BASELINE ?=

coverage:
	$(PYTEST) -m unit --cov --cov-report=term-missing --cov-report=json:build/coverage.json
	uv run python scripts/coverage_gate.py --report build/coverage.json $(if $(BASELINE),--baseline $(BASELINE))
