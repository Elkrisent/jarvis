"""scripts/traceability.py: parsing, marker discovery, result mapping, gating."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "traceability.py"

REQS = """\
| ID | Requirement | Phase |
|----|-------------|-------|
| REQ-1-1 | first | P0 |
| REQ-1-2 | has a pipe \\| inside | P0 |
| REQ-2-1 | later | P2 |
"""

TESTS = """\
import pytest

pytestmark = pytest.mark.req("REQ-1-1")


@pytest.mark.req("REQ-1-2", "REQ-2-1")
def test_a():
    pass


class TestK:
    @pytest.mark.req("REQ-2-1")
    async def test_b(self):
        pass

    def helper(self):
        pass
"""

INHERITED = """\
import pytest


class Base:
    def test_one(self):
        pass


@pytest.mark.req("REQ-1-2")
class TestImpl(Base):
    pass


@pytest.mark.req("REQ-2-1")
class TestHasOwn(Base):
    def test_own(self):
        pass
"""

JUNIT = """\
<testsuites><testsuite>
<testcase classname="x" name="test_a[1]"/>
<testcase classname="x" name="test_a[2]"><failure message="boom"/></testcase>
<testcase classname="x" name="test_b"><skipped/></testcase>
</testsuite></testsuites>
"""


@pytest.fixture(scope="module")
def trace() -> ModuleType:
    spec = importlib.util.spec_from_file_location("traceability", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["traceability"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "requirements.md").write_text(REQS)
    tests = tmp_path / "packages" / "core" / "tests" / "unit"
    tests.mkdir(parents=True)
    (tests / "test_x.py").write_text(TESTS)
    return tmp_path


def test_parse_requirements(trace: ModuleType) -> None:
    reqs = trace.parse_requirements(REQS)
    assert [(r.id, r.phase) for r in reqs] == [("REQ-1-1", 0), ("REQ-1-2", 0), ("REQ-2-1", 2)]
    assert reqs[1].text == "has a pipe \\| inside"


def test_duplicate_ids_are_rejected(trace: ModuleType) -> None:
    with pytest.raises(ValueError, match="REQ-1-1"):
        trace.parse_requirements("| REQ-1-1 | a | P0 |\n| REQ-1-1 | b | P0 |\n")


def test_markers_found_on_functions_classes_and_modules(trace: ModuleType, repo: Path) -> None:
    found = trace.collect_markers(trace.find_test_files(repo), repo)
    names = {k: sorted(t.name for t in v) for k, v in found.items()}
    assert names == {
        "REQ-1-1": ["TestK::test_b", "test_a"],  # module-level mark covers every test
        "REQ-1-2": ["test_a"],
        "REQ-2-1": ["TestK::test_b", "test_a"],
    }


def test_non_test_functions_are_ignored(trace: ModuleType, repo: Path) -> None:
    found = trace.collect_markers(trace.find_test_files(repo), repo)
    assert all(t.name != "TestK::helper" for refs in found.values() for t in refs)


def test_results_use_worst_outcome_across_parametrizations(
    trace: ModuleType, tmp_path: Path
) -> None:
    junit = tmp_path / "j.xml"
    junit.write_text(JUNIT)
    results = trace.read_results(junit)
    assert (results["test_a"], results["test_b"]) == ("fail", "skip")


def test_report_lists_tests_results_and_uncovered(trace: ModuleType, repo: Path) -> None:
    reqs = trace.parse_requirements(REQS + "| REQ-3-1 | nobody tests me | P3 |\n")
    found = trace.collect_markers(trace.find_test_files(repo), repo)
    report = trace.render(reqs, found, {"test_a": "pass", "test_b": "pass"})
    assert "3 of 4 requirements have at least one test" in report
    assert "| REQ-3-1 | P3 | **uncovered** | — |" in report
    assert "`packages/core/tests/unit/test_x.py::test_a`" in report
    assert "| REQ-1-2 | P0 | `packages/core/tests/unit/test_x.py::test_a` | pass |" in report


def test_report_without_junit_says_not_run(trace: ModuleType, repo: Path) -> None:
    reqs = trace.parse_requirements(REQS)
    found = trace.collect_markers(trace.find_test_files(repo), repo)
    assert "| not run |" in trace.render(reqs, found, None)


def test_unknown_requirement_in_a_marker_is_a_problem(trace: ModuleType, repo: Path) -> None:
    (repo / "packages/core/tests/unit/test_y.py").write_text(
        'import pytest\n\n@pytest.mark.req("REQ-9-9")\ndef test_y():\n    pass\n'
    )
    reqs = trace.parse_requirements(REQS)
    found = trace.collect_markers(trace.find_test_files(repo), repo)
    problems = trace.check(reqs, found, None)
    assert len(problems) == 1
    assert "REQ-9-9" in problems[0]


def test_completed_phase_gate_flags_only_uncovered_requirements_up_to_that_phase(
    trace: ModuleType, repo: Path
) -> None:
    reqs = trace.parse_requirements(
        REQS + "| REQ-0-9 | untested | P0 |\n| REQ-5-1 | untested | P5 |\n"
    )
    found = trace.collect_markers(trace.find_test_files(repo), repo)
    assert trace.check(reqs, found, None) == []
    assert trace.check(reqs, found, 2) == ["REQ-0-9 (P0) has no test"]


def test_main_writes_report_and_exit_code(
    trace: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert trace.main([], root=repo) == 0
    assert (repo / "docs" / "traceability.md").read_text().startswith("# Traceability")
    assert "3/3 requirements covered" in capsys.readouterr().out
    (repo / "docs" / "requirements.md").write_text(REQS + "| REQ-0-9 | untested | P0 |\n")
    assert trace.main(["--completed-phase", "0"], root=repo) == 1
    assert "REQ-0-9 (P0) has no test" in capsys.readouterr().err


def test_class_level_marks_cover_tests_inherited_from_a_base(trace: ModuleType, repo: Path) -> None:
    (repo / "packages/core/tests/unit/test_inh.py").write_text(INHERITED)
    found = trace.collect_markers(trace.find_test_files(repo), repo)
    inherited = [t for t in found["REQ-1-2"] if t.file.endswith("test_inh.py")]
    assert [(t.name, t.func) for t in inherited] == [("TestImpl", "TestImpl")]
    # A class with its own tests is still referenced per test, not per class.
    assert [t.name for t in found["REQ-2-1"] if t.file.endswith("test_inh.py")] == [
        "TestHasOwn::test_own"
    ]


def test_results_are_also_keyed_by_class(trace: ModuleType, tmp_path: Path) -> None:
    junit = tmp_path / "j.xml"
    junit.write_text(
        "<testsuites><testsuite>"
        '<testcase classname="pkg.mod.TestImpl" name="test_one"/>'
        '<testcase classname="pkg.mod.TestImpl" name="test_two"><failure message="x"/></testcase>'
        "</testsuite></testsuites>"
    )
    assert trace.read_results(junit)["TestImpl"] == "fail"
