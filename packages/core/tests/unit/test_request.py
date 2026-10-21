import pytest
from jarvis_core.request import AuthContext, NormalizedRequest

pytestmark = pytest.mark.unit


def _req(**overrides: object) -> NormalizedRequest:
    base: dict[str, object] = {
        "request_id": "01J8",
        "api_slug": "weather",
        "method": "GET",
        "path": "/v1/current",
        "received_at": 1.0,
    }
    return NormalizedRequest(**(base | overrides))  # type: ignore[arg-type]


def test_auth_context_without_restriction_allows_any_api() -> None:
    assert AuthContext(project_id="prj_1", key_id="key_1").allows("api_x")


def test_auth_context_restricted_to_listed_apis() -> None:
    ctx = AuthContext(project_id="prj_1", key_id="key_1", allowed_api_ids=frozenset({"api_a"}))
    assert ctx.allows("api_a")
    assert not ctx.allows("api_b")


def test_auth_context_is_immutable() -> None:
    ctx = AuthContext(project_id="prj_1", key_id="key_1")
    with pytest.raises(ValueError, match="frozen"):
        ctx.project_id = "other"  # type: ignore[misc]


@pytest.mark.parametrize(("method", "cacheable"), [("GET", True), ("head", True), ("POST", False)])
def test_cacheable_methods(method: str, cacheable: bool) -> None:
    assert _req(method=method).is_cacheable_method is cacheable


def test_sorted_query_is_order_independent() -> None:
    a = _req(query=(("b", "2"), ("a", "1")))
    b = _req(query=(("a", "1"), ("b", "2")))
    assert a.sorted_query == b.sorted_query == (("a", "1"), ("b", "2"))


def test_header_lookup_is_case_insensitive_on_the_name() -> None:
    req = _req(headers=(("accept", "application/json"),))
    assert req.header("Accept") == "application/json"
    assert req.header("x-missing") is None
