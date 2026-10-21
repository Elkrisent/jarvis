"""Meta test: collection fails when a test lacks a type marker."""

import os
from pathlib import Path

import pytest

ROOT_CONFTEST = (Path(__file__).parents[2] / "conftest.py").read_text()


@pytest.fixture(autouse=True)
def _isolate_from_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep pytest-cov's subprocess hooks from instrumenting the inner pytest run."""
    for var in [v for v in os.environ if v.startswith("COV_CORE_")]:
        monkeypatch.delenv(var)


PYTEST_INI = """
[pytest]
markers =
    unit
    integration
    system
    functional
    e2e
    chaos
    security
    perf
"""


@pytest.mark.unit
def test_unmarked_test_fails_collection(pytester: pytest.Pytester) -> None:
    pytester.makeini(PYTEST_INI)
    pytester.makeconftest(ROOT_CONFTEST.replace('pytest_plugins = ["pytester"]', ""))
    pytester.makepyfile("def test_naked():\n    assert True\n")
    result = pytester.runpytest_subprocess()
    assert result.ret != 0
    assert "Tests without a type marker" in result.stderr.str() + result.stdout.str()
    assert "test_naked" in result.stderr.str() + result.stdout.str()


@pytest.mark.unit
def test_marked_test_passes(pytester: pytest.Pytester) -> None:
    pytester.makeini(PYTEST_INI)
    pytester.makeconftest(ROOT_CONFTEST.replace('pytest_plugins = ["pytester"]', ""))
    pytester.makepyfile("import pytest\n\n@pytest.mark.unit\ndef test_ok():\n    assert True\n")
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)
