import pytest

from tests.conftest import compose, readyz, wait_until

pytestmark = [pytest.mark.system, pytest.mark.usefixtures("compose_stack")]

M1_1_TABLES = {
    "alembic_version",
    "api_dependencies",
    "api_policy_versions",
    "audit_log",
    "jarvis_keys",
    "projects",
    "secrets",
    "settings",
    "users",
}


@pytest.mark.req("REQ-12-2")
def test_api_applies_migrations_to_the_compose_postgres_at_startup() -> None:
    wait_until(
        lambda: r if (r := readyz("api")).status_code == 200 else None,
        timeout_s=60,
        what="api /readyz",
    )
    out = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "jarvis",
        "-d",
        "jarvis",
        "-Atc",
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
    ).stdout
    assert M1_1_TABLES <= set(out.split())
