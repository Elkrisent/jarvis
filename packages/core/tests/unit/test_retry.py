import pytest
from hypothesis import given
from hypothesis import strategies as st
from jarvis_core.clock import FakeClock, FakeRandom
from jarvis_core.config import RetryPolicy
from jarvis_core.deadline import Deadline
from jarvis_core.retry import (
    Failure,
    backoff_ceiling_s,
    backoff_delay_s,
    condition_retryable,
    decide,
    method_retryable,
    parse_retry_after,
)

pytestmark = pytest.mark.unit

METHODS = ["GET", "HEAD", "OPTIONS", "PUT", "DELETE", "POST", "PATCH"]
SAFE = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
STATUSES = [200, 201, 204, 301, 400, 401, 403, 404, 408, 409, 422, 429, 500, 501, 502, 503, 504]
RETRYABLE = {429, 502, 503, 504}
NOW = 1_000_000.0


def _decide(
    *,
    method: str = "GET",
    failure: Failure | None = None,
    policy: RetryPolicy | None = None,
    attempts_made: int = 1,
    elapsed: float = 0.0,
    total_ms: int = 5000,
    rng: FakeRandom | None = None,
):  # type: ignore[no-untyped-def]
    clock = FakeClock()
    deadline = Deadline(clock, clock.monotonic(), total_ms)
    clock.advance(elapsed)
    return decide(
        policy or RetryPolicy(),
        method=method,
        failure=failure or Failure("status", 503),
        attempts_made=attempts_made,
        deadline=deadline,
        min_attempt_ms=300,
        rng=rng or FakeRandom(sequence=[0.5]),
        now_epoch=NOW,
    )


@pytest.mark.req("REQ-4.7-4")
@pytest.mark.parametrize("method", METHODS)
def test_method_retryability_without_an_idempotency_header(method: str) -> None:
    assert method_retryable(method, None) is (method in SAFE)
    assert method_retryable(method.lower(), None) is (method in SAFE)


@pytest.mark.req("REQ-4.7-3")
@pytest.mark.parametrize("method", METHODS)
def test_every_method_is_retryable_with_an_idempotency_header(method: str) -> None:
    assert method_retryable(method, "Idempotency-Key") is True


@pytest.mark.req("REQ-4.7-1")
@pytest.mark.parametrize("status", STATUSES)
def test_status_conditions(status: int) -> None:
    assert condition_retryable(Failure("status", status)) is (status in RETRYABLE)


@pytest.mark.req("REQ-4.7-1")
@pytest.mark.parametrize("kind", ["connect", "timeout"])
def test_connection_errors_and_attempt_timeouts_are_retryable(kind: str) -> None:
    assert condition_retryable(Failure(kind)) is True  # type: ignore[arg-type]


@pytest.mark.req("REQ-4.7-1", "REQ-4.7-3", "REQ-4.7-4")
@pytest.mark.parametrize("idempotency", [None, "Idempotency-Key"])
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize(
    "failure",
    [Failure("connect"), Failure("timeout"), *(Failure("status", s) for s in STATUSES)],
)
def test_decision_table(failure: Failure, method: str, idempotency: str | None) -> None:
    expected = condition_retryable(failure) and (method in SAFE or idempotency is not None)
    policy = RetryPolicy(idempotency_header=idempotency)
    assert _decide(method=method, failure=failure, policy=policy).retry is expected


@pytest.mark.req("REQ-4.7-9")
@pytest.mark.parametrize(("made", "retry"), [(1, True), (2, True), (3, False), (4, False)])
def test_max_attempts_bounds_the_attempts(made: int, retry: bool) -> None:
    assert _decide(attempts_made=made, policy=RetryPolicy(max_attempts=3)).retry is retry


def test_max_attempts_of_one_never_retries() -> None:
    assert _decide(policy=RetryPolicy(max_attempts=1)).retry is False


def test_a_disabled_policy_never_retries() -> None:
    assert _decide(policy=RetryPolicy(enabled=False)).retry is False


@pytest.mark.req("REQ-4.7-6")
@pytest.mark.parametrize(
    ("index", "ceiling"), [(0, 0.1), (1, 0.2), (2, 0.4), (3, 0.8), (4, 1.6), (5, 2.0), (50, 2.0)]
)
def test_backoff_ceiling_doubles_to_the_cap(index: int, ceiling: float) -> None:
    assert backoff_ceiling_s(RetryPolicy(), index) == pytest.approx(ceiling)


@pytest.mark.req("REQ-4.7-6")
@given(
    base=st.integers(min_value=1, max_value=5000),
    extra=st.integers(min_value=0, max_value=20000),
    index=st.integers(min_value=0, max_value=200),
    draw=st.floats(min_value=0.0, max_value=1.0, exclude_max=True),
)
def test_backoff_delay_is_always_within_zero_and_the_ceiling(
    base: int, extra: int, index: int, draw: float
) -> None:
    policy = RetryPolicy(base_ms=base, cap_ms=base + extra)
    delay = backoff_delay_s(policy, index, FakeRandom(sequence=[draw]))
    assert 0.0 <= delay <= min(policy.cap_ms, policy.base_ms * 2 ** min(index, 32)) / 1000


@pytest.mark.req("REQ-4.7-6")
def test_the_default_backoff_uses_the_injected_randomness() -> None:
    first = _decide(rng=FakeRandom(sequence=[0.0]))
    assert first.retry
    assert first.delay_s == 0.0
    last = _decide(attempts_made=2, rng=FakeRandom(sequence=[0.999]))
    assert last.delay_s == pytest.approx(0.2 * 0.999)


@pytest.mark.req("REQ-4.7-2")
@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_seconds_is_honored_when_it_fits(status: int) -> None:
    result = _decide(failure=Failure("status", status, "2"))
    assert result.retry
    assert result.delay_s == 2.0


@pytest.mark.req("REQ-4.7-2")
@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_that_does_not_fit_the_deadline_means_no_retry(status: int) -> None:
    # 5 s budget; a retry needs the wait plus min_attempt (0.3 s) to fit in what remains.
    assert _decide(failure=Failure("status", status, "4"), elapsed=0.7).retry is True  # 0.3 left
    assert _decide(failure=Failure("status", status, "4"), elapsed=0.71).retry is False
    assert _decide(failure=Failure("status", status, "5"), elapsed=0.5).retry is False
    assert _decide(failure=Failure("status", status, "3600")).retry is False


@pytest.mark.req("REQ-4.7-2")
def test_retry_after_is_only_read_for_429_and_503() -> None:
    result = _decide(failure=Failure("status", 502, "3600"), rng=FakeRandom(sequence=[0.5]))
    assert result.retry
    assert result.delay_s == pytest.approx(0.05)  # normal backoff


@pytest.mark.req("REQ-4.7-2")
def test_a_malformed_retry_after_falls_back_to_backoff() -> None:
    result = _decide(failure=Failure("status", 503, "soon"), rng=FakeRandom(sequence=[0.5]))
    assert result.retry
    assert result.delay_s == pytest.approx(0.05)


def test_a_backoff_that_leaves_less_than_min_attempt_does_not_retry() -> None:
    policy = RetryPolicy(base_ms=1000, cap_ms=2000)
    rng = FakeRandom(sequence=[0.99])
    # 1 s ceiling * 0.99 = 0.99 s wait; remaining after 4.0 s elapsed is 1.0 s -> 0.01 s left
    assert _decide(policy=policy, elapsed=4.0, rng=rng).retry is False
    assert _decide(policy=policy, elapsed=3.0, rng=FakeRandom(sequence=[0.99])).retry is True


@pytest.mark.req("REQ-4.7-2")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0.0),
        ("120", 120.0),
        (" 7 ", 7.0),
        ("-1", None),
        ("1.5", None),
        ("", None),
        ("abc", None),
        ("99999999999", None),
        (None, None),
    ],
)
def test_parse_retry_after_delta_seconds(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value, NOW) == expected


@pytest.mark.req("REQ-4.7-2")
def test_parse_retry_after_http_date() -> None:
    # 1_000_000 s after the epoch is Mon, 12 Jan 1970 13:46:40 GMT
    assert parse_retry_after("Mon, 12 Jan 1970 13:46:50 GMT", NOW) == pytest.approx(10.0)
    assert parse_retry_after("Mon, 12 Jan 1970 13:46:30 GMT", NOW) == 0.0  # in the past
    assert parse_retry_after("Not, 99 Foo 1970 99:99:99 GMT", NOW) is None


@pytest.mark.req("REQ-4.7-2")
def test_a_date_without_a_zone_is_read_as_utc() -> None:
    assert parse_retry_after("Mon, 12 Jan 1970 13:46:50 -0000", NOW) == pytest.approx(10.0)
