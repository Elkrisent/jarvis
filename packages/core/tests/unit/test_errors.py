import pytest
from jarvis_core.errors import (
    ALL_ERRORS,
    BodyTooLargeError,
    CircuitOpenError,
    JarvisError,
    RateLimitedError,
    error_for_code,
)

pytestmark = pytest.mark.unit

# SPEC §4.4 lists exactly these codes.
SPEC_CODES = {
    "JV_AUTH_INVALID",
    "JV_RATE_LIMITED",
    "JV_QUOTA_EXCEEDED",
    "JV_CIRCUIT_OPEN",
    "JV_UPSTREAM_TIMEOUT",
    "JV_UPSTREAM_UNREACHABLE",
    "JV_BODY_TOO_LARGE",
    "JV_STREAMING_UNSUPPORTED",
    "JV_DESTINATION_BLOCKED",
}


@pytest.mark.req("REQ-4.4-6")
def test_every_spec_code_has_an_error_class() -> None:
    assert {cls.code for cls in ALL_ERRORS} == SPEC_CODES


def test_codes_are_unique() -> None:
    codes = [cls.code for cls in ALL_ERRORS]
    assert len(codes) == len(set(codes))


@pytest.mark.parametrize("cls", ALL_ERRORS, ids=lambda c: c.code)
def test_error_to_http_status_mapping_is_a_valid_error_status(cls: type[JarvisError]) -> None:
    assert issubclass(cls, JarvisError)
    assert 400 <= cls.http_status <= 599


@pytest.mark.parametrize(
    ("cls", "status"),
    [(RateLimitedError, 429), (CircuitOpenError, 503), (BodyTooLargeError, 413)],
)
def test_selected_status_mappings(cls: type[JarvisError], status: int) -> None:
    assert cls.http_status == status


@pytest.mark.req("REQ-4.4-5")
def test_body_matches_spec_shape() -> None:
    err = CircuitOpenError("api is down", request_id="01J8")
    assert err.to_body() == {
        "error": {"code": "JV_CIRCUIT_OPEN", "message": "api is down", "request_id": "01J8"}
    }


def test_message_defaults_to_code() -> None:
    assert str(RateLimitedError()) == "JV_RATE_LIMITED"


def test_error_for_code_round_trips() -> None:
    assert error_for_code("JV_CIRCUIT_OPEN") is CircuitOpenError


def test_error_for_unknown_code_raises() -> None:
    with pytest.raises(KeyError, match="JV_NOPE"):
        error_for_code("JV_NOPE")


MANAGEMENT_CODES = {"JV_AUTH_REQUIRED", "JV_NOT_FOUND", "JV_CONFLICT", "JV_NOT_IMPLEMENTED"}


def test_management_api_codes_are_separate_from_the_spec_codes() -> None:
    from jarvis_core.errors import MANAGEMENT_ERRORS

    assert {cls.code for cls in MANAGEMENT_ERRORS} == MANAGEMENT_CODES
    assert not {cls.code for cls in ALL_ERRORS} & MANAGEMENT_CODES
    assert {cls.code: cls.http_status for cls in MANAGEMENT_ERRORS} == {
        "JV_AUTH_REQUIRED": 401,
        "JV_NOT_FOUND": 404,
        "JV_CONFLICT": 409,
        "JV_NOT_IMPLEMENTED": 501,
    }


@pytest.mark.parametrize("code", sorted(MANAGEMENT_CODES))
def test_management_codes_round_trip(code: str) -> None:
    assert error_for_code(code).code == code
