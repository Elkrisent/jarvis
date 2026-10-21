.PHONY: help sync lint format typecheck \
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

test-unit:
	$(PYTEST) -m unit

test-integration:
	@echo "placeholder: no integration suites yet (arrives with M0.3/M1.1)"

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

coverage:
	$(PYTEST) -m unit --cov --cov-report=term-missing --cov-report=xml
