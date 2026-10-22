"""Repo-level facts the spec requires (REQ-16.1-1, REQ-16.3-1), read from the files."""

import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _workflow(name: str) -> dict[str, object]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())  # type: ignore[no-any-return]


def _triggers(workflow: dict[str, object]) -> dict[str, object]:
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on") or workflow[True]  # type: ignore[return-value,index]


def _run_steps(workflow: dict[str, object], job: str) -> str:
    steps = workflow["jobs"][job]["steps"]  # type: ignore[index]
    return "\n".join(s.get("run", "") for s in steps)


@pytest.fixture(scope="module")
def pyproject() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


# ------------------------------------------------------------------- REQ-16.1-1


@pytest.mark.req("REQ-16.1-1")
def test_uv_workspace_contains_every_package(pyproject: dict[str, object]) -> None:
    members = pyproject["tool"]["uv"]["workspace"]["members"]  # type: ignore[index]
    assert set(members) == {"packages/core", "services/*", "tools/mock_upstream"}
    for service in ("gateway", "api", "worker"):
        assert (ROOT / "services" / service / "pyproject.toml").exists()


@pytest.mark.req("REQ-16.1-1")
def test_mypy_is_strict_for_core_and_gateway_only(pyproject: dict[str, object]) -> None:
    overrides = pyproject["tool"]["mypy"]["overrides"]  # type: ignore[index]
    strict = [o for o in overrides if o.get("strict")]
    assert len(strict) == 1
    assert set(strict[0]["module"]) == {
        "jarvis_core.*",
        "jarvis_core",
        "jarvis_gateway.*",
        "jarvis_gateway",
    }


@pytest.mark.req("REQ-16.1-1")
def test_ruff_and_pytest_asyncio_are_configured(pyproject: dict[str, object]) -> None:
    tool = pyproject["tool"]
    assert "lint" in tool["ruff"]  # type: ignore[index]
    assert tool["pytest"]["ini_options"]["asyncio_mode"] == "auto"  # type: ignore[index]
    dev = " ".join(pyproject["dependency-groups"]["dev"])  # type: ignore[index]
    for needed in ("ruff==", "mypy==", "pytest==", "pytest-asyncio=="):
        assert needed in dev


@pytest.mark.req("REQ-16.1-1")
def test_every_dependency_is_pinned(pyproject: dict[str, object]) -> None:
    deps = list(pyproject["dependency-groups"]["dev"])  # type: ignore[index]
    for pkg in (
        "packages/core",
        "services/gateway",
        "services/api",
        "services/worker",
        "tools/mock_upstream",
    ):
        project = tomllib.loads((ROOT / pkg / "pyproject.toml").read_text())["project"]
        deps += project.get("dependencies", [])
    unpinned = [d for d in deps if "==" not in d and not d.startswith("jarvis-")]
    assert unpinned == []


# ------------------------------------------------------------------- REQ-16.3-1


@pytest.mark.req("REQ-16.3-1")
def test_ci_runs_on_pull_requests_and_pushes_to_main() -> None:
    triggers = _triggers(_workflow("ci.yml"))
    assert "pull_request" in triggers
    assert triggers["push"]["branches"] == ["main"]  # type: ignore[index]


@pytest.mark.req("REQ-16.3-1")
def test_ci_runs_lint_typecheck_unit_and_integration_tests() -> None:
    ci = _workflow("ci.yml")
    python_job = _run_steps(ci, "python")
    for target in ("make lint", "make typecheck", "make test-unit"):
        assert target in python_job
    # Coverage counts integration tests too, so the gates run where the service containers are.
    integration_job = _run_steps(ci, "integration")
    # `make coverage` runs the integration tests itself, so a separate run would only repeat them.
    assert "make coverage" in integration_job
    assert "make test-integration" not in integration_job


@pytest.mark.req("REQ-16.3-1")
def test_ci_integration_job_has_postgres_and_redis_service_containers() -> None:
    services = _workflow("ci.yml")["jobs"]["integration"]["services"]  # type: ignore[index]
    assert set(services) == {"postgres", "redis"}
    assert "timescaledb" in services["postgres"]["image"]
    assert services["redis"]["image"].startswith("redis:")


@pytest.mark.req("REQ-16.3-1")
def test_ci_enforces_coverage_gates_through_the_make_target() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert "scripts/coverage_gate.py" in makefile.split("coverage:", 1)[1]


@pytest.mark.req("REQ-16.3-1")
def test_the_three_main_workflows_exist_with_their_jobs() -> None:
    assert set(_workflow("ci.yml")["jobs"]) == {"python", "integration"}  # type: ignore[arg-type]
    assert {"bandit", "pip-audit", "gitleaks", "trivy", "security-tests"} <= set(
        _workflow("security.yml")["jobs"]  # type: ignore[arg-type]
    )
    assert "system" in _workflow("system.yml")["jobs"]  # type: ignore[operator]


def test_third_party_actions_are_pinned_to_a_version_or_commit() -> None:
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        for line in path.read_text().splitlines():
            if "uses:" in line and "@" in line:
                ref = line.split("@", 1)[1].split()[0]
                where = f"{path.name}: {line.strip()}"
                assert ref, where
                assert ref not in ("main", "master", "latest"), where
