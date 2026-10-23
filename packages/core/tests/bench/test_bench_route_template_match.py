"""Micro-benchmark: templating a realistic path (metric ``route_template_match``)."""

import pytest
from jarvis_core.route_template import RouteTemplater
from pytest_benchmark.fixture import BenchmarkFixture

pytestmark = pytest.mark.perf

PATTERNS = ["/v1/users/{id}/orders", "/v1/search", "/v1/items/{sku}/reviews/{review}"]
FALLBACK_PATH = "/v1/users/12345/profile/550e8400-e29b-41d4-a716-446655440000"
EXPLICIT_PATH = "/v1/items/abc-9/reviews/77"


def test_bench_route_template_match(benchmark: BenchmarkFixture) -> None:
    templater = RouteTemplater(PATTERNS)

    def match() -> None:
        templater.template(FALLBACK_PATH)
        templater.template(EXPLICIT_PATH)

    benchmark(match)
