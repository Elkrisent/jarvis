import pytest
from jarvis_gateway.auth import MAX_KEY_LENGTH, extract_key

pytestmark = pytest.mark.unit

KEY = "jv_live_abc123"


@pytest.mark.req("REQ-4.2-2")
def test_x_jarvis_key_header_is_accepted_case_insensitively() -> None:
    assert extract_key([("X-JARVIS-key", KEY)], "authorization") == KEY


@pytest.mark.req("REQ-4.2-2")
def test_the_header_wins_over_a_jv_value_in_the_slot() -> None:
    headers = [("authorization", "Bearer jv_live_other"), ("x-jarvis-key", KEY)]
    assert extract_key(headers, "authorization") == KEY


def test_an_invalid_header_is_not_retried_from_the_slot() -> None:
    headers = [("x-jarvis-key", "garbage"), ("authorization", f"Bearer {KEY}")]
    assert extract_key(headers, "authorization") == "garbage"


def test_repeated_header_is_ambiguous_and_yields_no_key() -> None:
    headers = [("x-jarvis-key", KEY), ("x-jarvis-key", "jv_live_other")]
    assert extract_key(headers, "authorization") is None


def test_blank_header_falls_through_to_the_slot() -> None:
    headers = [("x-jarvis-key", "  "), ("authorization", f"Bearer {KEY}")]
    assert extract_key(headers, "authorization") == KEY


@pytest.mark.req("REQ-4.2-3")
@pytest.mark.parametrize(
    "value", [f"Bearer {KEY}", f"bEaReR   {KEY}", f"  {KEY}  ", KEY, f"BEARER\t{KEY}"]
)
def test_slot_value_with_jv_prefix_is_accepted(value: str) -> None:
    assert extract_key([("authorization", value)], "authorization") == KEY


@pytest.mark.req("REQ-4.2-3")
@pytest.mark.parametrize("value", ["Bearer sk-abc", f"Basic {KEY}", "", "   ", "Bearer "])
def test_slot_value_without_jv_prefix_is_no_key(value: str) -> None:
    assert extract_key([("authorization", value)], "authorization") is None


def test_a_custom_slot_header_is_read() -> None:
    assert extract_key([("x-api-key", KEY)], "x-api-key") == KEY


def test_no_slot_means_header_only() -> None:
    assert extract_key([("authorization", f"Bearer {KEY}")], None) is None


def test_missing_everything_is_no_key() -> None:
    assert extract_key([], "authorization") is None


@pytest.mark.parametrize("name", ["x-jarvis-key", "authorization"])
def test_oversized_values_are_no_key(name: str) -> None:
    value = "jv_" + "a" * MAX_KEY_LENGTH
    assert extract_key([(name, value)], "authorization") is None
