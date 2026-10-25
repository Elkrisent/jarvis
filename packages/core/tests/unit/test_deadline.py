import pytest
from hypothesis import given
from hypothesis import strategies as st
from jarvis_core.clock import FakeClock
from jarvis_core.deadline import Deadline

pytestmark = pytest.mark.unit


def _deadline(clock: FakeClock, total_ms: int = 5000, started: float | None = None) -> Deadline:
    return Deadline(clock, clock.monotonic() if started is None else started, total_ms)


def test_remaining_counts_down_from_the_start_on_the_injected_clock() -> None:
    clock = FakeClock()
    deadline = _deadline(clock)
    assert deadline.remaining() == 5.0
    clock.advance(1.25)
    assert deadline.remaining() == pytest.approx(3.75)


def test_the_clock_starts_at_ingress_not_when_the_deadline_is_built() -> None:
    clock = FakeClock()
    ingress = clock.monotonic()
    clock.advance(0.4)  # time spent before the pipeline built the deadline
    assert _deadline(clock, started=ingress).remaining() == pytest.approx(4.6)


@pytest.mark.req("REQ-4.6-5")
def test_remaining_never_goes_below_zero_and_expired_is_true_at_the_edge() -> None:
    clock = FakeClock()
    deadline = _deadline(clock, total_ms=1000)
    clock.advance(0.999)
    assert not deadline.expired
    clock.advance(0.001)
    assert deadline.remaining() == 0.0
    assert deadline.expired
    clock.advance(10)
    assert deadline.remaining() == 0.0
    assert deadline.expired


@pytest.mark.req("REQ-4.6-5")
@pytest.mark.parametrize(
    ("elapsed", "attempt_s", "expected"),
    [
        (0.0, 2.0, 2.0),  # plenty of time: the attempt timeout applies
        (2.9, 2.0, 2.0),  # remaining 2.1 > 2.0
        (3.0, 2.0, 2.0),  # remaining exactly the attempt timeout
        (3.5, 2.0, 1.5),  # the deadline is tighter: min(attempt, remaining)
        (4.999, 2.0, 0.001),
        (5.0, 2.0, 0.0),
        (9.0, 2.0, 0.0),
    ],
)
def test_attempt_timeout_is_min_of_attempt_and_remaining(
    elapsed: float, attempt_s: float, expected: float
) -> None:
    clock = FakeClock()
    deadline = _deadline(clock)
    clock.advance(elapsed)
    assert deadline.attempt_timeout(attempt_s) == pytest.approx(expected)


@pytest.mark.req("REQ-4.6-3")
@pytest.mark.parametrize(
    ("elapsed", "can_start"),
    [
        (0.0, True),
        (4.6, True),  # remaining 0.4 >= 0.3
        (4.7, True),  # remaining exactly min_attempt_ms
        (4.7001, False),  # a hair under
        (4.9, False),
        (5.0, False),
        (7.0, False),
    ],
)
def test_a_new_attempt_needs_at_least_min_attempt_ms_remaining(
    elapsed: float, can_start: bool
) -> None:
    clock = FakeClock()
    deadline = _deadline(clock)
    clock.advance(elapsed)
    assert deadline.can_start_attempt(300) is can_start


def test_a_deadline_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        Deadline(FakeClock(), 0.0, 0)


@pytest.mark.req("REQ-4.6-5")
@given(
    total_ms=st.integers(min_value=1, max_value=60_000),
    attempt_ms=st.integers(min_value=1, max_value=60_000),
    min_ms=st.integers(min_value=1, max_value=60_000),
    waits=st.lists(st.floats(min_value=0, max_value=30, allow_nan=False), max_size=30),
)
def test_attempts_never_run_past_the_deadline(
    total_ms: int, attempt_ms: int, min_ms: int, waits: list[float]
) -> None:
    """Any sequence of attempt latencies: an attempt only starts with time left and is always
    bounded by what remains, so the clock never passes the deadline by running attempts."""
    clock = FakeClock()
    deadline = _deadline(clock, total_ms=total_ms)
    for wait in waits:
        if not deadline.can_start_attempt(min_ms):
            break
        bound = deadline.attempt_timeout(attempt_ms / 1000)
        assert 0.0 <= bound <= deadline.remaining()
        clock.advance(min(wait, bound))  # an attempt takes its latency or is cut at the bound
        if deadline.expired:
            break
    assert clock.monotonic() <= total_ms / 1000 + 1e-9
