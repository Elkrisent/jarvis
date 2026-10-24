import pytest
from jarvis_core.route_template import DEFAULT_MAX_TEMPLATES, OTHER, RouteTemplater

pytestmark = pytest.mark.unit

UUID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.req("REQ-4.14-3")
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/users/42", "/v1/users/{id}"),
        (f"/v1/items/{UUID}", "/v1/items/{id}"),
        (f"/v1/items/{UUID.upper()}", "/v1/items/{id}"),
        ("/v1/blobs/" + "a" * 25, "/v1/blobs/{id}"),
        ("/v1/blobs/" + "9f86d081884c7d659a2feaa0c55ad015", "/v1/blobs/{id}"),
        ("/v1/blobs/" + "Zm9vYmFyLWJhemJhei1xdXV4LXF1dXg_", "/v1/blobs/{id}"),
        ("/v1/blobs/" + "a" * 24, "/v1/blobs/" + "a" * 24),
        ("/v1/users/me", "/v1/users/me"),
        ("/v1/users/١٢٣", "/v1/users/١٢٣"),
        (
            "/v1/users/42/orders/9f86d081884c7d659a2feaa0c55ad015",
            "/v1/users/{id}/orders/{id}",
        ),
    ],
)
@pytest.mark.req("REQ-2-11")
def test_fallback_rules(path: str, expected: str) -> None:
    assert RouteTemplater().template(path) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/a/", "/v1/a"),
        ("//v1//a", "/v1/a"),
        ("", "/"),
        ("/", "/"),
        ("/v1/a?x=1#f", "/v1/a"),
        ("/v1/a#f?x=1", "/v1/a"),
        ("?x=1", "/"),
    ],
)
def test_normalisation(path: str, expected: str) -> None:
    assert RouteTemplater().template(path) == expected


@pytest.mark.req("REQ-4.14-1", "REQ-4.14-2")
def test_explicit_pattern_groups_different_raw_paths() -> None:
    templater = RouteTemplater(["/v1/users/{id}/orders"])
    assert templater.template("/v1/users/7/orders") == "/v1/users/{id}/orders"
    assert templater.template("/v1/users/abc/orders") == "/v1/users/{id}/orders"
    assert templater.template("/v1/users/abc/orders/") == "/v1/users/{id}/orders"


@pytest.mark.req("REQ-4.14-2", "REQ-2-11")
def test_explicit_pattern_wins_over_the_fallback() -> None:
    assert RouteTemplater(["/v1/users/{name}"]).template("/v1/users/42") == "/v1/users/{name}"


@pytest.mark.req("REQ-4.14-2")
def test_a_literal_pattern_beats_a_placeholder_pattern() -> None:
    templater = RouteTemplater(["/v1/users/{user}", "/v1/users/me"])
    assert templater.template("/v1/users/me") == "/v1/users/me"
    assert templater.template("/v1/users/bob") == "/v1/users/{user}"


def test_ties_go_to_the_configured_order() -> None:
    templater = RouteTemplater(["/v1/{a}/x", "/v1/{b}/x"])
    assert templater.template("/v1/q/x") == "/v1/{a}/x"


def test_a_pattern_that_does_not_match_falls_back() -> None:
    templater = RouteTemplater(["/v1/users/{id}/orders"])
    assert templater.template("/v1/users/42") == "/v1/users/{id}"
    assert templater.template("/v2/users/42/orders") == "/v2/users/{id}/orders"
    assert templater.template("/v1/users/42/orders/1") == "/v1/users/{id}/orders/{id}"


def test_patterns_are_case_sensitive_and_a_placeholder_needs_a_segment() -> None:
    templater = RouteTemplater(["/v1/Users/{id}"])
    assert templater.template("/v1/users/abc") == "/v1/users/abc"
    assert templater.template("/v1/Users/") == "/v1/Users"


def test_a_partial_brace_segment_is_literal() -> None:
    templater = RouteTemplater(["/v1/x-{id}"])
    assert templater.template("/v1/x-5") == "/v1/x-5"
    assert templater.template("/v1/x-{id}") == "/v1/x-{id}"


@pytest.mark.req("REQ-4.14-4")
def test_new_templates_over_the_cap_go_to_other() -> None:
    templater = RouteTemplater(max_templates=3)
    assert [templater.template(p) for p in ("/a", "/b", "/c")] == ["/a", "/b", "/c"]
    assert templater.template("/d") == OTHER
    assert templater.template("/a") == "/a"
    assert templater.template("/d") == OTHER


@pytest.mark.req("REQ-4.14-4")
def test_the_default_cap_is_500() -> None:
    assert DEFAULT_MAX_TEMPLATES == 500
    templater = RouteTemplater()
    assert all(templater.template(f"/p{i}") == f"/p{i}" for i in range(500))
    assert templater.template("/p500") == OTHER


@pytest.mark.req("REQ-4.14-4")
def test_explicit_patterns_are_never_replaced_by_other() -> None:
    templater = RouteTemplater(["/v1/users/{id}"], max_templates=1)
    assert templater.template("/x") == "/x"
    assert templater.template("/y") == OTHER
    assert templater.template("/v1/users/5") == "/v1/users/{id}"


def test_ids_collapse_so_they_do_not_use_up_the_cap() -> None:
    templater = RouteTemplater(max_templates=2)
    assert {templater.template(f"/v1/users/{i}") for i in range(1000)} == {"/v1/users/{id}"}
    assert templater.template("/health") == "/health"


@pytest.mark.parametrize("path", ["/" + "a/" * 40, "/" + "x" * 2049, "/" + "a" * 100_000])
def test_hostile_paths_go_to_other_without_using_the_cap(path: str) -> None:
    templater = RouteTemplater(max_templates=1)
    assert templater.template(path) == OTHER
    assert templater.template("/ok") == "/ok"
