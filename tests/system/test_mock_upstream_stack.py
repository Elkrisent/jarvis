import httpx
import pytest

from tests.conftest import MOCK_UPSTREAM_URL, MockUpstream, wait_until

pytestmark = [pytest.mark.system, pytest.mark.usefixtures("compose_stack")]


def test_mock_upstream_is_healthy_in_the_test_profile() -> None:
    r = wait_until(
        lambda: httpx.get(f"{MOCK_UPSTREAM_URL}/__admin/health", timeout=3),
        timeout_s=60,
        what="mock upstream health",
    )
    assert r.json() == {"status": "ok"}


def test_rules_and_call_log_work_through_the_container(mock_upstream: MockUpstream) -> None:
    mock_upstream.set_rules({"path_prefix": "/v1", "status": 429, "retry_after_s": 2})
    r = httpx.get(f"{MOCK_UPSTREAM_URL}/v1/current", params={"city": "Bangalore"}, timeout=5)
    assert (r.status_code, r.headers["retry-after"]) == (429, "2")
    (call,) = mock_upstream.calls()
    assert call["path"] == "/v1/current"
    assert call["query"] == [["city", "Bangalore"]]


def test_the_stack_can_reach_the_mock_upstream_by_service_name(mock_upstream: MockUpstream) -> None:
    from tests.conftest import compose

    out = compose(
        "exec",
        "-T",
        "api",
        "python",
        "-c",
        "import urllib.request; print(urllib.request.urlopen('http://mock-upstream:8080/ping').status)",
    ).stdout.strip()
    assert out == "200"
    assert [c["path"] for c in mock_upstream.calls()] == ["/ping"]
