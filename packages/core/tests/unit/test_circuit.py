import pytest
from hypothesis import given
from hypothesis import strategies as st
from jarvis_core.circuit import (
    admit,
    half_opened,
    is_failure,
    probe_failed,
    probe_succeeded,
    tripped,
    window_s,
)
from jarvis_core.config import CircuitPolicy
from jarvis_core.interfaces import CallStats, CircuitSnapshot, CircuitState

pytestmark = pytest.mark.unit

POLICY = CircuitPolicy()  # 50 %, 30 s window, 20 calls, 30 s open, 300 s max
CLOSED = CircuitSnapshot()
OPEN_AT_100 = CircuitSnapshot(CircuitState.OPEN, opened_at=100.0, open_duration_s=30)


@pytest.mark.req("REQ-4.8-2", "REQ-4.8-3")
@pytest.mark.parametrize(
    ("kwargs", "failure"),
    [
        ({"status": 200}, False),
        ({"status": 204}, False),
        ({"status": 301}, False),
        ({"status": 400}, False),
        ({"status": 404}, False),
        ({"status": 429}, False),  # rate limits are the caller's problem, not an outage
        ({"status": 499}, False),
        ({"status": 500}, True),
        ({"status": 502}, True),
        ({"status": 503}, True),
        ({"status": 504}, True),
        ({"status": 599}, True),
        ({"status": None, "timed_out": True}, True),
        ({"status": None, "connect_error": True}, True),
    ],
)
def test_what_counts_as_a_failure(kwargs: dict[str, object], failure: bool) -> None:
    assert is_failure(**kwargs) is failure  # type: ignore[arg-type]


@pytest.mark.req("REQ-4.8-4")
@pytest.mark.parametrize(
    ("total", "failures", "opens"),
    [
        (19, 19, False),  # all failing but under the min_calls floor
        (20, 9, False),  # 45 %
        (20, 10, True),  # exactly 50 %
        (20, 20, True),
        (100, 49, False),
        (100, 50, True),
        (0, 0, False),
    ],
)
def test_closed_to_open_needs_the_rate_and_the_min_calls(
    total: int, failures: int, opens: bool
) -> None:
    result = tripped(POLICY, CLOSED, CallStats(total, failures), now=500.0)
    if opens:
        assert result == CircuitSnapshot(CircuitState.OPEN, opened_at=500.0, open_duration_s=30)
    else:
        assert result is None


def test_only_a_closed_circuit_can_trip() -> None:
    assert tripped(POLICY, OPEN_AT_100, CallStats(100, 100), now=110.0) is None
    assert tripped(POLICY, half_opened(OPEN_AT_100), CallStats(100, 100), now=110.0) is None


def test_the_thresholds_come_from_the_policy() -> None:
    policy = CircuitPolicy(failure_rate_threshold=0.8, min_calls=5)
    assert tripped(policy, CLOSED, CallStats(5, 3), now=1.0) is None
    assert tripped(policy, CLOSED, CallStats(5, 4), now=1.0) is not None


@pytest.mark.req("REQ-4.8-10")
def test_a_closed_circuit_allows_everything() -> None:
    assert admit(POLICY, CLOSED, now=0.0).action == "allow"


@pytest.mark.req("REQ-4.8-5", "REQ-4.8-10")
@pytest.mark.parametrize(
    ("now", "action"),
    [(100.0, "reject"), (129.999, "reject"), (130.0, "probe"), (500.0, "probe")],
)
def test_open_rejects_until_open_duration_has_passed(now: float, action: str) -> None:
    result = admit(POLICY, OPEN_AT_100, now)
    assert result.action == action
    assert result.state is CircuitState.OPEN


@pytest.mark.req("REQ-4.8-6")
def test_half_open_wants_a_single_probe() -> None:
    result = admit(POLICY, half_opened(OPEN_AT_100), now=131.0)
    assert result.action == "probe"
    assert result.state is CircuitState.HALF_OPEN


def test_half_open_keeps_what_the_open_state_knew() -> None:
    assert half_opened(OPEN_AT_100) == CircuitSnapshot(
        CircuitState.HALF_OPEN, opened_at=100.0, open_duration_s=30
    )


@pytest.mark.req("REQ-4.8-7")
def test_a_successful_probe_closes_the_circuit_and_forgets_older_calls() -> None:
    closed = probe_succeeded(now=140.0)
    assert closed.state is CircuitState.CLOSED
    assert closed.closed_at == 140.0
    assert admit(POLICY, closed, now=140.0).action == "allow"


@pytest.mark.req("REQ-4.8-8")
def test_a_failed_probe_reopens_for_twice_as_long_up_to_the_cap() -> None:
    snap = half_opened(OPEN_AT_100)
    durations = []
    for step in range(7):
        snap = probe_failed(POLICY, snap, now=200.0 + step)
        assert snap.state is CircuitState.OPEN
        assert snap.opened_at == 200.0 + step
        durations.append(snap.open_duration_s)
        snap = half_opened(snap)
    assert durations == [60, 120, 240, 300, 300, 300, 300]


def test_a_close_resets_the_open_duration_for_the_next_trip() -> None:
    snap = probe_failed(POLICY, half_opened(OPEN_AT_100), now=200.0)
    assert snap.open_duration_s == 60
    snap = probe_succeeded(now=300.0)
    reopened = tripped(POLICY, snap, CallStats(20, 20), now=400.0)
    assert reopened is not None
    assert reopened.open_duration_s == 30


@pytest.mark.req("REQ-4.8-4")
@pytest.mark.parametrize(
    ("closed_at", "now", "expected"),
    [
        (None, 1000.0, 30),
        (990.0, 1000.0, 10),
        (999.2, 1000.0, 1),
        (1000.0, 1000.0, 1),
        (0.0, 1000.0, 30),
    ],
)
def test_the_window_never_reaches_back_before_the_last_close(
    closed_at: float | None, now: float, expected: int
) -> None:
    snap = CircuitSnapshot(CircuitState.CLOSED, closed_at=closed_at)
    assert window_s(POLICY, snap, now) == expected


@pytest.mark.req("REQ-4.8-4", "REQ-4.8-5", "REQ-4.8-7", "REQ-4.8-8")
@given(
    steps=st.lists(
        st.tuples(
            st.sampled_from(["calls", "admit", "probe_ok", "probe_fail"]),
            st.integers(min_value=0, max_value=100),
            st.integers(min_value=0, max_value=100),
            st.floats(min_value=0, max_value=200),
        ),
        max_size=60,
    )
)
def test_no_sequence_of_outcomes_reaches_an_illegal_state(
    steps: list[tuple[str, int, int, float]],
) -> None:
    policy = CircuitPolicy()
    snap = CLOSED
    now = 0.0
    for kind, total, failed, dt in steps:
        now += dt
        before = snap
        if kind == "calls":
            stats = CallStats(total, min(failed, total))
            snap = tripped(policy, snap, stats, now) or snap
        elif kind == "admit":
            if admit(policy, snap, now).action == "probe" and snap.state is CircuitState.OPEN:
                snap = half_opened(snap)
        elif snap.state is CircuitState.HALF_OPEN:
            snap = probe_succeeded(now) if kind == "probe_ok" else probe_failed(policy, snap, now)
        assert snap.state in CircuitState
        # Only the legal edges.
        legal = {
            (CircuitState.CLOSED, CircuitState.CLOSED),
            (CircuitState.CLOSED, CircuitState.OPEN),
            (CircuitState.OPEN, CircuitState.OPEN),
            (CircuitState.OPEN, CircuitState.HALF_OPEN),
            (CircuitState.HALF_OPEN, CircuitState.HALF_OPEN),
            (CircuitState.HALF_OPEN, CircuitState.CLOSED),
            (CircuitState.HALF_OPEN, CircuitState.OPEN),
        }
        assert (before.state, snap.state) in legal
        if snap.state is CircuitState.OPEN:
            assert snap.opened_at is not None
            assert policy.open_duration_s <= snap.open_duration_s <= policy.max_open_duration_s
