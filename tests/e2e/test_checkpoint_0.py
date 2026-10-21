"""Checkpoint 0 acceptance stories (IMPLEMENTATION, Phase 0).

1. Given a clean machine, when I clone and run `docker compose up -d`, all services are healthy.
2. Given a PR with a failing test, when CI runs, then the merge is blocked.
3. Given a push to main, when `bench.yml` runs, then a data point appears on the gh-pages chart.
"""

import json
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from tests.conftest import REPO, github_headers, github_repo

pytestmark = pytest.mark.e2e


# ------------------------------------------------------------------------ story 1


def test_story_1_fresh_clone_comes_up_all_healthy(tmp_path: Path) -> None:
    clone = tmp_path / "jarvis"
    subprocess.run(["git", "clone", "-q", str(REPO), str(clone)], check=True)
    deploy = clone / "deploy"
    (deploy / ".env").write_text((deploy / ".env.example").read_text())

    def compose(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", "-p", "jarvis-e2e", *args],
            check=True,
            capture_output=True,
            text=True,
            cwd=deploy,
        )

    try:
        compose("up", "-d", "--build", "--wait", "--wait-timeout", "180")
        rows = [json.loads(line) for line in compose("ps", "--format", "json").stdout.splitlines()]
    finally:
        subprocess.run(
            ["docker", "compose", "-p", "jarvis-e2e", "down", "-v", "--remove-orphans"],
            check=False,
            capture_output=True,
            cwd=deploy,
        )
    services = ("postgres", "redis", "gateway", "api", "worker")
    assert {r["Service"] for r in rows} == set(services)
    assert {r["Service"]: r["Health"] for r in rows} == dict.fromkeys(services, "healthy")


# ------------------------------------------------------------------------ story 2

REQUIRED_CHECKS = {
    "python",
    "integration",
    "bandit",
    "pip-audit",
    "gitleaks",
    "trivy (gateway)",
    "trivy (api)",
    "trivy (worker)",
    "security-tests",
}


def _job_names() -> set[str]:
    """Check names the workflows produce, with matrix jobs expanded the way GitHub names them."""
    names: set[str] = set()
    for path in (REPO / ".github" / "workflows").glob("*.yml"):
        for job_id, job in yaml.safe_load(path.read_text())["jobs"].items():
            matrix = job.get("strategy", {}).get("matrix", {})
            if matrix:
                (values,) = matrix.values()
                names |= {f"{job_id} ({v})" for v in values}
            else:
                names.add(job.get("name", job_id))
    return names


@pytest.mark.req("REQ-16.3-2")
def test_story_2_main_is_protected_so_a_failing_pr_cannot_merge() -> None:
    repo = github_repo()
    api = f"https://api.github.com/repos/{repo}"
    headers = github_headers()
    summaries = httpx.get(f"{api}/rulesets", headers=headers, timeout=15).raise_for_status().json()
    active = [r for r in summaries if r["enforcement"] == "active" and r["target"] == "branch"]
    assert active, "no active branch ruleset"
    rulesets = [
        httpx.get(f"{api}/rulesets/{r['id']}", headers=headers, timeout=15)
        .raise_for_status()
        .json()
        for r in active
    ]

    covering = [r for r in rulesets if "~DEFAULT_BRANCH" in r["conditions"]["ref_name"]["include"]]
    assert covering, "no active ruleset targets the default branch"
    rules = {rule["type"]: rule for r in covering for rule in r["rules"]}
    assert {"pull_request", "required_status_checks", "non_fast_forward", "deletion"} <= set(rules)

    params = rules["required_status_checks"]["parameters"]
    required = {c["context"] for c in params["required_status_checks"]}
    assert required == REQUIRED_CHECKS
    assert params["strict_required_status_checks_policy"] is True
    assert all(not r.get("bypass_actors") for r in covering)

    # a required check that no workflow produces would make every PR wait forever
    assert required <= _job_names()


# ------------------------------------------------------------------------ story 3


def test_story_3_bench_chart_has_a_data_point_on_gh_pages() -> None:
    owner, name = github_repo().split("/")
    url = f"https://{owner}.github.io/{name}/bench/load/data.js"
    text = httpx.get(url, timeout=15, follow_redirects=True).raise_for_status().text
    data = json.loads(text[text.index("{") :])
    runs = [run for entries in data["entries"].values() for run in entries]
    assert runs, "no benchmark runs published"
    names = {b["name"] for run in runs for b in run["benches"]}
    assert {"mock_direct_p50", "mock_direct_p99"} <= names


def test_budgets_for_the_phase_are_all_met_in_the_recorded_checkpoint() -> None:
    """Checkpoint rule: a missed budget in scope for the phase blocks the checkpoint."""
    snapshot = json.loads((REPO / "benchmarks" / "checkpoints" / "P0.json").read_text())
    budgets = yaml.safe_load((REPO / "benchmarks" / "budgets.yaml").read_text())["metrics"]
    for metric, value in snapshot["results"].items():
        budget = budgets[metric]["budget"]
        if budget is None:
            continue
        limit_ok = value <= budget if budgets[metric]["direction"] == "lower" else value >= budget
        assert limit_ok, f"{metric}={value} violates budget {budget}"
