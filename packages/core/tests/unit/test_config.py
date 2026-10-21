import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from jarvis_core.config import ApiDependency, CircuitPolicy, Credential, RetryPolicy
from pydantic import ValidationError

pytestmark = pytest.mark.unit

# SPEC §5.1, verbatim.
SPEC_EXAMPLE = json.loads("""
{
  "slug": "weather",
  "name": "Weather API",
  "base_url": "https://api.weather.example",
  "credential": { "mode": "query", "param_name": "appid", "secret_ref": "sec_01H..." },
  "jarvis_key_slot": "x-jarvis-key",
  "deadline_ms": 5000,
  "attempt_timeout_ms": 2000,
  "retry": {
    "enabled": true, "max_attempts": 3, "base_ms": 100, "cap_ms": 2000,
    "budget_ratio": 0.2, "idempotency_header": null
  },
  "circuit": {
    "failure_rate_threshold": 0.5, "window_s": 30, "min_calls": 20,
    "open_duration_s": 30, "max_open_duration_s": 300
  },
  "cache": {
    "enabled": true, "default_ttl_s": 60, "stale_ttl_s": 600,
    "honor_cache_control": false, "vary_headers": ["accept", "accept-language"],
    "max_object_bytes": 1048576
  },
  "rate_limits": {
    "inbound":  { "enabled": false, "capacity": 100, "refill_per_s": 10 },
    "upstream": { "enabled": true,  "capacity": 60,  "refill_per_s": 1 }
  },
  "fallback": { "serve_stale": true, "fallback_api": null },
  "routes": [
    { "pattern": "/v1/current", "cache_ttl_s": 120 },
    { "pattern": "/v1/forecast/{city}", "cache_ttl_s": 900 }
  ],
  "body_sampling": { "enabled": false, "rate": 0.0 }
}
""")


def _minimal(**overrides: Any) -> dict[str, Any]:
    return {"slug": "weather", "name": "Weather", "base_url": "https://api.example.com"} | overrides


def _invalid(match: str, **overrides: Any) -> None:
    with pytest.raises(ValidationError, match=match):
        ApiDependency.model_validate(_minimal(**overrides))


# ------------------------------------------------------------------ valid configs


@pytest.mark.req("REQ-5.1-1")
def test_spec_example_is_valid_and_round_trips() -> None:
    api = ApiDependency.model_validate(SPEC_EXAMPLE)
    assert api.cache.enabled is True
    assert api.rate_limits.upstream.capacity == 60
    assert [r.pattern for r in api.routes] == ["/v1/current", "/v1/forecast/{city}"]
    assert ApiDependency.model_validate(api.model_dump(mode="json")) == api


@pytest.mark.req("REQ-4.6-1")
def test_minimal_config_gets_spec_defaults() -> None:
    api = ApiDependency.model_validate(_minimal())
    assert (api.deadline_ms, api.attempt_timeout_ms, api.min_attempt_ms) == (5000, 2000, 300)
    assert (api.retry.base_ms, api.retry.cap_ms, api.retry.budget_ratio) == (100, 2000, 0.2)
    assert api.circuit.failure_rate_threshold == 0.5
    assert (api.circuit.window_s, api.circuit.min_calls) == (30, 20)
    assert (api.circuit.open_duration_s, api.circuit.max_open_duration_s) == (30, 300)
    assert api.cache.vary_headers == ("accept", "accept-language")
    assert api.cache.max_object_bytes == 1_048_576
    assert api.credential.mode == "none"
    assert api.jarvis_key_slot == "x-jarvis-key"


def test_base_url_trailing_slash_is_stripped() -> None:
    assert ApiDependency.model_validate(_minimal(base_url="https://a.example/v1/")).base_url == (
        "https://a.example/v1"
    )


def test_vary_headers_are_lowercased_and_deduplicated() -> None:
    api = ApiDependency.model_validate(
        _minimal(cache={"vary_headers": ["Accept", "accept", "X-A"]})
    )
    assert api.cache.vary_headers == ("accept", "x-a")


def test_key_slot_is_lowercased() -> None:
    assert ApiDependency.model_validate(
        _minimal(jarvis_key_slot="Authorization")
    ).jarvis_key_slot == ("authorization")


# ------------------------------------------------------------- cross-field rules


@pytest.mark.req("REQ-5.1-2")
def test_attempt_timeout_must_not_exceed_deadline() -> None:
    _invalid("attempt_timeout_ms must be <= deadline_ms", deadline_ms=1000, attempt_timeout_ms=1001)


def test_attempt_timeout_equal_to_deadline_is_allowed() -> None:
    ApiDependency.model_validate(_minimal(deadline_ms=1000, attempt_timeout_ms=1000))


def test_min_attempt_must_fit_in_deadline() -> None:
    _invalid(
        "min_attempt_ms must be <= deadline_ms",
        deadline_ms=200,
        attempt_timeout_ms=100,
        min_attempt_ms=300,
    )


@pytest.mark.req("REQ-5.1-3")
def test_retry_cap_must_be_at_least_base() -> None:
    _invalid("cap_ms must be >= base_ms", retry={"base_ms": 500, "cap_ms": 499})


def test_retry_cap_equal_to_base_is_allowed() -> None:
    RetryPolicy(base_ms=500, cap_ms=500)


@pytest.mark.req("REQ-5.1-4")
@pytest.mark.parametrize("ratio", [0, 0.0, -0.1, 1.0001, 2])
def test_retry_budget_ratio_out_of_range_is_rejected(ratio: float) -> None:
    with pytest.raises(ValidationError):
        RetryPolicy(budget_ratio=ratio)


@pytest.mark.parametrize("ratio", [0.0001, 0.5, 1.0])
def test_retry_budget_ratio_in_range_is_accepted(ratio: float) -> None:
    assert RetryPolicy(budget_ratio=ratio).budget_ratio == ratio


@pytest.mark.req("REQ-4.8-11")
def test_circuit_max_open_must_be_at_least_open() -> None:
    with pytest.raises(ValidationError, match="max_open_duration_s"):
        CircuitPolicy(open_duration_s=60, max_open_duration_s=30)


@pytest.mark.parametrize("rate", [0.0, -0.1])
def test_circuit_threshold_must_be_positive(rate: float) -> None:
    with pytest.raises(ValidationError):
        CircuitPolicy(failure_rate_threshold=rate)


@pytest.mark.req("REQ-4.12-5")
def test_fallback_api_cannot_be_self() -> None:
    _invalid("fallback_api must not point", fallback={"fallback_api": "weather"})


def test_fallback_api_to_another_api_is_allowed() -> None:
    ApiDependency.model_validate(_minimal(fallback={"fallback_api": "weather-backup"}))


def test_duplicate_route_patterns_are_rejected() -> None:
    _invalid("unique", routes=[{"pattern": "/a"}, {"pattern": "/a", "cache_ttl_s": 5}])


def test_route_pattern_must_start_with_slash() -> None:
    _invalid("must start with '/'", routes=[{"pattern": "v1/x"}])


def test_body_sampling_rate_bounds() -> None:
    _invalid("less than or equal to 1", body_sampling={"enabled": True, "rate": 1.5})


# ------------------------------------------------------------------- credentials


@pytest.mark.req("REQ-4.5-1")
@pytest.mark.parametrize(
    "good",
    [
        {"mode": "none"},
        {"mode": "bearer", "secret_ref": "sec_1"},
        {"mode": "header", "header_name": "X-Api-Key", "secret_ref": "sec_1"},
        {"mode": "query", "param_name": "appid", "secret_ref": "sec_1"},
    ],
)
def test_valid_credential_modes(good: dict[str, str]) -> None:
    assert Credential.model_validate(good).mode == good["mode"]


@pytest.mark.req("REQ-4.5-1")
@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"mode": "bearer"}, "secret_ref is required"),
        ({"mode": "header", "secret_ref": "s"}, "header_name is required"),
        ({"mode": "query", "secret_ref": "s"}, "param_name is required"),
        ({"mode": "none", "secret_ref": "s"}, "not allowed when mode is 'none'"),
        ({"mode": "bearer", "secret_ref": "s", "header_name": "X"}, "header_name is only allowed"),
        ({"mode": "bearer", "secret_ref": "s", "param_name": "p"}, "param_name is only allowed"),
        ({"mode": "digest", "secret_ref": "s"}, "mode"),
    ],
)
def test_invalid_credential_combinations(bad: dict[str, str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Credential.model_validate(bad)


# ------------------------------------------------------------- field-level checks


@pytest.mark.parametrize("slug", ["", "Weather", "-a", "a-", "a_b", "a b", "x" * 65])
def test_bad_slugs_are_rejected(slug: str) -> None:
    _invalid("slug", slug=slug)


@pytest.mark.parametrize("slug", ["a", "weather", "my-api-2", "x" * 64])
def test_good_slugs_are_accepted(slug: str) -> None:
    assert ApiDependency.model_validate(_minimal(slug=slug)).slug == slug


@pytest.mark.parametrize(
    "url",
    [
        "ftp://a.example",
        "api.example.com",
        "https://",
        "https://user:pw@a.example",
        "https://a.example?x=1",
        "https://a.example#frag",
    ],
)
def test_bad_base_urls_are_rejected(url: str) -> None:
    _invalid("base_url", base_url=url)


def test_unknown_fields_are_rejected() -> None:
    _invalid("Extra inputs", surprise=1)


@pytest.mark.parametrize("field", ["deadline_ms", "attempt_timeout_ms", "min_attempt_ms"])
@pytest.mark.parametrize("value", [0, -1])
def test_timings_must_be_positive(field: str, value: int) -> None:
    _invalid(field, **{field: value})


@pytest.mark.parametrize("value", [0, 11])
def test_max_attempts_bounds(value: int) -> None:
    _invalid("max_attempts", retry={"max_attempts": value})


@pytest.mark.req("REQ-4.11-7")
def test_rate_limit_refill_must_be_positive() -> None:
    _invalid("refill_per_s", rate_limits={"inbound": {"refill_per_s": 0}})


# ----------------------------------------------------------- property-based tests


@given(
    deadline=st.integers(min_value=1, max_value=600_000),
    attempt=st.integers(min_value=1, max_value=600_000),
)
def test_property_attempt_vs_deadline(deadline: int, attempt: int) -> None:
    data = _minimal(deadline_ms=deadline, attempt_timeout_ms=attempt, min_attempt_ms=1)
    if attempt <= deadline:
        ApiDependency.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            ApiDependency.model_validate(data)


@given(
    base=st.integers(min_value=1, max_value=10**6), cap=st.integers(min_value=1, max_value=10**6)
)
def test_property_cap_vs_base(base: int, cap: int) -> None:
    if cap >= base:
        assert RetryPolicy(base_ms=base, cap_ms=cap).cap_ms == cap
    else:
        with pytest.raises(ValidationError):
            RetryPolicy(base_ms=base, cap_ms=cap)


@given(ratio=st.floats(allow_nan=False, allow_infinity=False, min_value=-10, max_value=10))
def test_property_budget_ratio_in_half_open_unit_interval(ratio: float) -> None:
    if 0 < ratio <= 1:
        assert RetryPolicy(budget_ratio=ratio).budget_ratio == ratio
    else:
        with pytest.raises(ValidationError):
            RetryPolicy(budget_ratio=ratio)


@given(ratio=st.one_of(st.just(float("nan")), st.just(float("inf")), st.just(float("-inf"))))
def test_property_non_finite_ratio_is_rejected(ratio: float) -> None:
    with pytest.raises(ValidationError):
        RetryPolicy(budget_ratio=ratio)


@given(
    open_s=st.integers(min_value=1, max_value=10**5),
    max_open=st.integers(min_value=1, max_value=10**5),
    threshold=st.floats(min_value=0.001, max_value=1.0),
)
def test_property_circuit_durations(open_s: int, max_open: int, threshold: float) -> None:
    kwargs = {
        "open_duration_s": open_s,
        "max_open_duration_s": max_open,
        "failure_rate_threshold": threshold,
    }
    if max_open >= open_s:
        assert CircuitPolicy(**kwargs).max_open_duration_s == max_open
    else:
        with pytest.raises(ValidationError):
            CircuitPolicy(**kwargs)
