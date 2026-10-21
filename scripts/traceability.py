#!/usr/bin/env python3
"""Generate docs/traceability.md: requirement -> tests -> last result (IMPLEMENTATION §B.4).

Requirements come from docs/requirements.md. Tests are found statically by reading
``@pytest.mark.req("REQ-…")`` markers, so no test code is imported. Last results come from an
optional JUnit XML file (``pytest --junit-xml``).

Exit status is non-zero when a marker names an unknown requirement, or, with
``--completed-phase N``, when any requirement of phase N or earlier has no test.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("packages", "services", "tools", "tests")
SKIP_PARTS = {".venv", "node_modules", "__pycache__", ".git"}

_ROW = re.compile(r"^\|\s*(REQ-[\w.]+-\d+)\s*\|\s*(.*?)\s*\|\s*P(\d)\s*\|\s*$")


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    phase: int


@dataclass(frozen=True)
class TestRef:
    file: str
    name: str  # "Class::func" or "func"; a bare class/module name for class- or module-level marks
    func: str  # the function (or class) name used to look up results


def parse_requirements(text: str) -> list[Requirement]:
    reqs = [
        Requirement(m[1], m[2], int(m[3])) for line in text.splitlines() if (m := _ROW.match(line))
    ]
    ids = [r.id for r in reqs]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate requirement IDs: {', '.join(dupes)}")
    return reqs


def _req_ids(node: ast.expr) -> list[str]:
    """IDs from ``pytest.mark.req("A", "B")``; [] for any other expression."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return []
    f = node.func
    if f.attr != "req" or not (isinstance(f.value, ast.Attribute) and f.value.attr == "mark"):
        return []
    return [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def _marks(node: ast.expr) -> list[str]:
    if isinstance(node, ast.List | ast.Tuple):
        return [i for elt in node.elts for i in _req_ids(elt)]
    return _req_ids(node)


def collect_markers(paths: list[Path], root: Path) -> dict[str, list[TestRef]]:
    found: dict[str, list[TestRef]] = defaultdict(list)
    for path in paths:
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(), filename=rel)
        module_ids: list[str] = []
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets
            ):
                module_ids += _marks(stmt.value)

        def visit(body: list[ast.stmt], prefix: str, inherited: list[str], rel: str = rel) -> None:
            for stmt in body:
                if isinstance(stmt, ast.ClassDef):
                    ids = inherited + [i for d in stmt.decorator_list for i in _req_ids(d)]
                    has_own_tests = any(
                        isinstance(b, ast.FunctionDef | ast.AsyncFunctionDef)
                        and b.name.startswith("test")
                        for b in stmt.body
                    )
                    if ids and stmt.bases and not has_own_tests:
                        # The tests are inherited from a base class (e.g. a contract suite).
                        for req_id in dict.fromkeys(ids):
                            found[req_id].append(TestRef(rel, f"{prefix}{stmt.name}", stmt.name))
                    visit(stmt.body, f"{prefix}{stmt.name}::", ids)
                elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                    if not stmt.name.startswith("test"):
                        continue
                    own = [i for d in stmt.decorator_list for i in _req_ids(d)]
                    for req_id in dict.fromkeys(inherited + own):
                        found[req_id].append(TestRef(rel, f"{prefix}{stmt.name}", stmt.name))

        visit(tree.body, "", module_ids)
    return found


def find_test_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        base = root / d
        if base.is_dir():
            files += [
                p
                for p in sorted(base.rglob("*.py"))
                if not SKIP_PARTS & set(p.parts)
                and (
                    p.name.startswith("test_")
                    or p.name.endswith("_test.py")
                    or p.name == "contracts.py"
                )
            ]
    return files


def read_results(junit: Path) -> dict[str, str]:
    """Map test function name -> 'pass' | 'fail' | 'skip' (worst result across parametrizations)."""
    rank = {"pass": 0, "skip": 1, "fail": 2}
    out: dict[str, str] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        name = case.get("name", "").split("[")[0]
        # Also key by the test class, for class-level references to inherited tests.
        keys = [name, case.get("classname", "").rsplit(".", 1)[-1]]
        if case.find("failure") is not None or case.find("error") is not None:
            status = "fail"
        elif case.find("skipped") is not None:
            status = "skip"
        else:
            status = "pass"
        for key in filter(None, keys):
            if key not in out or rank[status] > rank[out[key]]:
                out[key] = status
    return out


def _cell(req_id: str, refs: list[TestRef], results: dict[str, str] | None) -> str:
    if not refs:
        return "—"
    if results is None:
        return "not run"
    seen = {results[r.func] for r in refs if r.func in results}
    for status in ("fail", "skip", "pass"):
        if status in seen:
            return status
    return "not run"


def render(
    reqs: list[Requirement],
    found: dict[str, list[TestRef]],
    results: dict[str, str] | None,
) -> str:
    covered = sum(1 for r in reqs if found.get(r.id))
    lines = [
        "# Traceability",
        "",
        "Generated by `make traceability`. Do not edit by hand.",
        "",
        f"**{covered} of {len(reqs)} requirements have at least one test.**",
        "",
        "| Requirement | Phase | Tests | Last result |",
        "|-------------|-------|-------|-------------|",
    ]
    for r in reqs:
        refs = found.get(r.id, [])
        tests = "<br>".join(f"`{t.file}::{t.name}`" for t in refs) if refs else "**uncovered**"
        lines.append(f"| {r.id} | P{r.phase} | {tests} | {_cell(r.id, refs, results)} |")
    return "\n".join(lines) + "\n"


def check(
    reqs: list[Requirement], found: dict[str, list[TestRef]], completed_phase: int | None
) -> list[str]:
    problems = [
        f"unknown requirement {i!r} used by {found[i][0].file}::{found[i][0].name}"
        for i in sorted(set(found) - {r.id for r in reqs})
    ]
    if completed_phase is not None:
        problems += [
            f"{r.id} (P{r.phase}) has no test"
            for r in reqs
            if r.phase <= completed_phase and not found.get(r.id)
        ]
    return problems


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--junit", type=Path, help="JUnit XML from a pytest run, for the last-result column"
    )
    ap.add_argument("--out", type=Path, default=Path("docs/traceability.md"))
    ap.add_argument("--requirements", type=Path, default=Path("docs/requirements.md"))
    ap.add_argument(
        "--completed-phase",
        type=int,
        help="fail if a requirement of this phase or earlier is uncovered",
    )
    args = ap.parse_args(argv)

    reqs = parse_requirements((root / args.requirements).read_text())
    found = collect_markers(find_test_files(root), root)
    results = read_results(args.junit) if args.junit else None

    out = args.out if args.out.is_absolute() else root / args.out
    out.write_text(render(reqs, found, results))

    problems = check(reqs, found, args.completed_phase)
    for p in problems:
        print(f"traceability: {p}", file=sys.stderr)
    covered = sum(1 for r in reqs if found.get(r.id))
    print(f"traceability: {covered}/{len(reqs)} requirements covered -> {out.relative_to(root)}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
