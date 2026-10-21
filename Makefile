.PHONY: help sync lint format typecheck security-scan bandit pip-audit \
	test-unit test-integration test-system test-functional test-e2e test-chaos \
	test-security test-web test-all bench-micro bench-load bench-soak \
	bench-checkpoint traceability coverage

PYTEST = uv run pytest

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

# Exit code 5 means "no tests collected": allowed only while no integration suite exists yet.
test-integration:
	$(PYTEST) -m integration; rc=$$?; if [ $$rc -eq 5 ]; then echo "no integration tests collected yet"; exit 0; fi; exit $$rc

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
	@echo "placeholder: benchmark infrastructure arrives in M0.6"

bench-load:
	@echo "placeholder: benchmark infrastructure arrives in M0.6"

bench-soak:
	@echo "placeholder: benchmark infrastructure arrives in M0.6"

bench-checkpoint:
	@echo "placeholder: benchmark infrastructure arrives in M0.6"

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
