"""Root pytest configuration.

Enforces that every collected test carries exactly the type markers the
implementation guide requires (IMPLEMENTATION.md §B.2, M0.1 meta test).
"""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]

TYPE_MARKERS = frozenset(
    {"unit", "integration", "system", "functional", "e2e", "chaos", "security", "perf"}
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Fail collection if any test lacks a test-type marker."""
    missing = [
        item.nodeid for item in items if not TYPE_MARKERS & {m.name for m in item.iter_markers()}
    ]
    if missing:
        listing = "\n  ".join(missing)
        raise pytest.UsageError(
            f"Tests without a type marker ({', '.join(sorted(TYPE_MARKERS))}):\n  {listing}"
        )
