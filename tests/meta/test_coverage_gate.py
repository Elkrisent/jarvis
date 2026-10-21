"""scripts/coverage_gate.py: gates from IMPLEMENTATION §B.3 and the 1-point drop rule."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "coverage_gate.py"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("coverage_gate", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["coverage_gate"] = module
    spec.loader.exec_module(module)
    return module


def _file(
    stmts: int, covered: int, branches: int = 0, covered_branches: int = 0
) -> dict[str, object]:
    return {
        "summary": {
            "num_statements": stmts,
            "covered_lines": covered,
            "num_branches": branches,
            "covered_branches": covered_branches,
        }
    }


def _report(
    core: tuple[int, int, int, int] = (100, 100, 20, 20), **others: tuple[int, int, int, int]
) -> dict[str, object]:
    files = {"packages/core/src/jarvis_core/a.py": _file(*core)}
    for name, vals in others.items():
        files[f"services/{name}/src/x/a.py"] = _file(*vals)
    return {"files": files}


def test_everything_at_100_percent_passes(gate: ModuleType) -> None:
    table, problems = gate.evaluate(_report())
    assert problems == []
    assert "| packages/core | 100.0% | 95% | 100.0% | 90% | — |" in table


def test_packages_without_code_are_reported_not_failed(gate: ModuleType) -> None:
    table, problems = gate.evaluate(_report())
    assert problems == []
    assert "| services/api | no code |" in table


@pytest.mark.parametrize(
    ("core", "needle"),
    [
        ((100, 94, 20, 20), "line coverage 94.0% < 95%"),
        ((100, 100, 20, 17), "branch coverage 85.0% < 90%"),
    ],
)
def test_core_below_gate_fails(
    gate: ModuleType, core: tuple[int, int, int, int], needle: str
) -> None:
    _, problems = gate.evaluate(_report(core))
    assert any(needle in p for p in problems)


def test_exactly_at_the_gate_passes(gate: ModuleType) -> None:
    _, problems = gate.evaluate(_report((100, 95, 20, 18)))
    assert problems == []


def test_each_package_has_its_own_gate(gate: ModuleType) -> None:
    _, problems = gate.evaluate(
        _report(gateway=(100, 89, 0, 0), api=(100, 85, 0, 0), worker=(100, 84, 0, 0))
    )
    assert [p.split(":")[0] for p in problems] == ["services/gateway", "services/worker"]


def test_files_in_a_package_are_combined(gate: ModuleType) -> None:
    report = {
        "files": {
            "packages/core/src/a.py": _file(50, 50),
            "packages/core/src/b.py": _file(50, 40),
        }
    }
    assert gate.measure(report, "packages/core/src/").line == 90.0


def test_drop_of_more_than_one_point_vs_baseline_fails(gate: ModuleType) -> None:
    baseline = _report((1000, 1000, 100, 100))
    now = _report((1000, 980, 100, 100))  # still above the 95% gate, but -2.0 points
    _, problems = gate.evaluate(now, baseline)
    assert len(problems) == 1
    assert "dropped more than 1 point" in problems[0]


def test_drop_of_exactly_one_point_is_allowed(gate: ModuleType) -> None:
    _, problems = gate.evaluate(_report((1000, 990, 100, 100)), _report((1000, 1000, 100, 100)))
    assert problems == []


def test_improvement_is_shown_in_the_table(gate: ModuleType) -> None:
    table, _ = gate.evaluate(_report((1000, 1000, 100, 100)), _report((1000, 980, 100, 98)))
    assert "+2.0 / +2.0" in table


def test_main_exit_code_and_step_summary(
    gate: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = tmp_path / "c.json"
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report.write_text(json.dumps(_report()))
    assert gate.main(["--report", str(report)]) == 0
    assert "### Coverage" in summary.read_text()
    report.write_text(json.dumps(_report((100, 10, 0, 0))))
    assert gate.main(["--report", str(report)]) == 1
    assert "line coverage 10.0%" in capsys.readouterr().err
