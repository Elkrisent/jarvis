"""Shared helpers for the benchmark scripts (IMPLEMENTATION Part C)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

Direction = Literal["lower", "higher"]
Status = Literal["ok", "over budget", "deferred", "no budget", "unknown metric"]


@dataclass(frozen=True)
class Budget:
    name: str
    unit: str
    direction: Direction
    budget: float | None
    spec: str
    # The phase from which a miss fails the checkpoint; earlier phases report it as "deferred".
    enforce_from_phase: int = 0


def load_budgets(path: Path) -> dict[str, Budget]:
    raw = yaml.safe_load(path.read_text())["metrics"]
    out: dict[str, Budget] = {}
    for name, item in raw.items():
        if item["direction"] not in ("lower", "higher"):
            raise ValueError(f"{name}: direction must be 'lower' or 'higher'")
        budget = item.get("budget")
        out[name] = Budget(
            name,
            item["unit"],
            item["direction"],
            None if budget is None else float(budget),
            item.get("spec", ""),
            int(item.get("enforce_from_phase", 0)),
        )
    return out


def judge(value: float, budget: Budget | None, phase: int | None = None) -> Status:
    """``phase`` is the checkpoint being judged; without it every budget is enforced."""
    if budget is None:
        return "unknown metric"
    if budget.budget is None:
        return "no budget"
    ok = value <= budget.budget if budget.direction == "lower" else value >= budget.budget
    if ok:
        return "ok"
    if phase is not None and phase < budget.enforce_from_phase:
        return "deferred"
    return "over budget"


def micro_results(pytest_benchmark_json: dict[str, Any]) -> dict[str, float]:
    """Mean time per op in microseconds, keyed by metric name (``test_bench_`` prefix removed)."""
    out: dict[str, float] = {}
    for bench in pytest_benchmark_json["benchmarks"]:
        name = bench["name"].removeprefix("test_bench_")
        out[name] = bench["stats"]["mean"] * 1_000_000
    return out


def k6_trend(summary: dict[str, Any], metric: str = "http_req_duration") -> dict[str, float]:
    """The trend stats of a k6 ``--summary-export`` metric (``med``, ``p(95)``, ...)."""
    return dict(summary["metrics"][metric])


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
