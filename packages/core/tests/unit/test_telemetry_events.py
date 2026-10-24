import json
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st
from jarvis_core.telemetry import (
    EVENT_FIELDS,
    SCHEMA_VERSION,
    RequestEvent,
    encode,
    event_from_outcome,
    format_ts,
)

pytestmark = pytest.mark.unit

SECTION_6_1_KEYS = [
    "v",
    "request_id",
    "ts",
    "project_id",
    "key_id",
    "api_id",
    "method",
    "route",
    "status",
    "source",
    "cache",
    "cache_key_hash",
    "attempts",
    "latency_ms",
    "upstream_latency_ms",
    "req_bytes",
    "resp_bytes",
    "circuit",
    "error_code",
    "dedup_role",
]
IDS = {"project_id": "prj_1", "key_id": "key_1", "api_id": "api_1"}
BASE = {
    "request_id": "01J8ZZZZZZZZZZZZZZZZZZZZZZ",
    "ts": "2026-09-24T10:42:01.123Z",
    "method": "GET",
    "route": "/v1/forecast/{city}",
    "latency_ms": 3,
    "req_bytes": 0,
    "resp_bytes": 5120,
}


def _event(**overrides: object) -> RequestEvent:
    args: dict[str, object] = {
        **BASE,
        **IDS,
        "status": 200,
        "source": "upstream",
        "attempts": 1,
        "upstream_latency_ms": 2,
        "error_code": None,
        **overrides,
    }
    return event_from_outcome(**args)  # type: ignore[arg-type]


@pytest.mark.req("REQ-6.1-2")
def test_event_has_exactly_the_section_6_1_keys_plus_kind_in_order() -> None:
    body = _event().to_dict()
    assert list(body) == ["kind", *SECTION_6_1_KEYS]
    assert body["v"] == SCHEMA_VERSION == 1
    assert body["kind"] == "request"
    assert [name for name, _ in EVENT_FIELDS] == ["kind", *SECTION_6_1_KEYS]


@pytest.mark.req("REQ-6.1-2")
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, {"status": 200, "source": "upstream", "attempts": 1, "error_code": None}),
        ({"status": 503}, {"status": 503, "source": "upstream", "error_code": None}),
        (
            {"status": 504, "error_code": "JV_UPSTREAM_TIMEOUT", "upstream_latency_ms": None},
            {"source": "upstream", "attempts": 1, "upstream_latency_ms": None},
        ),
        (
            {"status": 502, "error_code": "JV_UPSTREAM_UNREACHABLE", "upstream_latency_ms": None},
            {"error_code": "JV_UPSTREAM_UNREACHABLE"},
        ),
        (
            {
                "status": 401,
                "error_code": "JV_AUTH_INVALID",
                "source": "jarvis",
                "attempts": 0,
                "upstream_latency_ms": None,
                "project_id": None,
                "key_id": None,
                "api_id": None,
                "route": None,
            },
            {"project_id": None, "key_id": None, "api_id": None, "route": None, "attempts": 0},
        ),
        (
            {"status": 413, "error_code": "JV_BODY_TOO_LARGE", "source": "jarvis", "attempts": 0},
            {"status": 413},
        ),
        (
            {
                "status": 400,
                "error_code": "JV_STREAMING_UNSUPPORTED",
                "source": "jarvis",
                "attempts": 0,
            },
            {"status": 400},
        ),
        (
            {"status": 503, "error_code": "JV_NOT_READY", "source": "jarvis", "attempts": 0},
            {"status": 503},
        ),
        (
            {"status": 404, "error_code": "JV_API_NOT_FOUND", "source": "jarvis", "attempts": 0},
            {"status": 404},
        ),
        (
            {
                "status": 403,
                "error_code": "JV_DESTINATION_BLOCKED",
                "source": "jarvis",
                "attempts": 0,
            },
            {"status": 403},
        ),
    ],
)
def test_event_for_every_outcome(overrides: dict[str, object], expected: dict[str, object]) -> None:
    body = _event(**overrides).to_dict()
    for key, value in expected.items():
        assert body[key] == value
    assert body["cache"] is None
    assert body["cache_key_hash"] is None
    assert body["circuit"] is None
    assert body["dedup_role"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"latency_ms": -1},
        {"latency_ms": 1.5},
        {"upstream_latency_ms": -1},
        {"req_bytes": -1},
        {"resp_bytes": -1},
        {"attempts": -1},
        {"status": 99},
        {"status": 600},
        {"latency_ms": True},
        {"source": "cache"},
        {"error_code": "oops"},
        {"error_code": ""},
        {"method": ""},
    ],
)
def test_invalid_values_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"must|status|source|method|error_code"):
        _event(**overrides)


def test_encode_is_compact_ascii_json_that_round_trips() -> None:
    event = _event(route="/v1/ünï/{id}")
    text = encode(event)
    assert " " not in text.replace("ünï", "")  # compact separators
    assert text.isascii()
    assert json.loads(text) == event.to_dict()


@pytest.mark.parametrize(
    ("epoch", "expected"),
    [
        (0.0, "1970-01-01T00:00:00.000Z"),
        (1_790_000_000.1239, "2026-09-21T14:13:20.123Z"),
        (1_790_000_000.9999, "2026-09-21T14:13:20.999Z"),
    ],
)
def test_format_ts_is_utc_milliseconds(epoch: float, expected: str) -> None:
    assert format_ts(epoch) == expected
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", format_ts(epoch))


@given(
    status=st.integers(100, 599),
    latency=st.integers(0, 10**9),
    sent=st.integers(0, 10**12),
    received=st.integers(0, 10**12),
    route=st.sampled_from(["/v1/a/{id}", "/", "{other}", None]),
)
def test_any_valid_input_round_trips(
    status: int, latency: int, sent: int, received: int, route: str | None
) -> None:
    event = _event(
        status=status, latency_ms=latency, req_bytes=sent, resp_bytes=received, route=route
    )
    decoded = json.loads(encode(event))
    assert decoded == event.to_dict()
    assert decoded["status"] == status
