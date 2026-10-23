import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jarvis_core.route_template import OTHER, RouteTemplater

pytestmark = pytest.mark.unit

PATTERNS = ["/v1/users/{id}", "/v1/users/me", "/health"]
paths = st.text(alphabet=st.characters(blacklist_categories=["Cs"]), max_size=3000)
segments = st.text(alphabet="abcdef0123456789-_/?#", max_size=12)
random_paths = st.lists(segments, max_size=6).map(lambda parts: "/" + "/".join(parts))


@settings(max_examples=200)
@given(path=paths)
def test_templating_never_raises(path: str) -> None:
    result = RouteTemplater(PATTERNS).template(path)
    assert isinstance(result, str)
    assert result


@settings(max_examples=200)
@given(path=paths)
def test_the_output_is_stable_for_the_same_input(path: str) -> None:
    assert RouteTemplater(PATTERNS).template(path) == RouteTemplater(PATTERNS).template(path)
    templater = RouteTemplater(PATTERNS)
    assert templater.template(path) == templater.template(path)


@settings(max_examples=100)
@given(batch=st.lists(random_paths, min_size=1, max_size=500))
def test_the_cap_holds_under_random_paths(batch: list[str]) -> None:
    templater = RouteTemplater(PATTERNS, max_templates=20)
    outputs = {templater.template(p) for p in batch}
    assert len(outputs - set(PATTERNS)) <= 20 + 1
    assert all(o in set(PATTERNS) | {OTHER} or o.startswith("/") for o in outputs)
