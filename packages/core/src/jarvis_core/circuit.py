"""The circuit breaker state machine (SPEC §4.8), pure and driven by explicit timestamps.

States move CLOSED → OPEN → HALF_OPEN → CLOSED, or HALF_OPEN → OPEN with a longer open duration.
Nothing here does I/O: the gateway feeds in the stored snapshot, call statistics and the time,
and stores whatever snapshot comes back.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from jarvis_core.config import CircuitPolicy
from jarvis_core.interfaces import CallStats, CircuitSnapshot, CircuitState


@dataclass(frozen=True)
class Admission:
    """``allow``: send normally. ``reject``: fail fast. ``probe``: the circuit wants one probe;
    the caller must win the probe lock, then move the circuit to HALF_OPEN and send it."""

    action: Literal["allow", "probe", "reject"]
    state: CircuitState


def is_failure(*, status: int | None, timed_out: bool = False, connect_error: bool = False) -> bool:
    """5xx, timeouts and connection errors count; every other status, 4xx included, does not."""
    return timed_out or connect_error or (status is not None and status >= 500)


def admit(policy: CircuitPolicy, snapshot: CircuitSnapshot, now: float) -> Admission:
    if snapshot.state is CircuitState.CLOSED:
        return Admission("allow", CircuitState.CLOSED)
    if snapshot.state is CircuitState.OPEN:
        opened_at = snapshot.opened_at if snapshot.opened_at is not None else now
        duration = snapshot.open_duration_s or policy.open_duration_s
        if now >= opened_at + duration:
            return Admission("probe", CircuitState.OPEN)
        return Admission("reject", CircuitState.OPEN)
    return Admission("probe", CircuitState.HALF_OPEN)


def window_s(policy: CircuitPolicy, snapshot: CircuitSnapshot, now: float) -> int:
    """The window to count over: ``window_s``, but never reaching back before the last close."""
    if snapshot.closed_at is None:
        return policy.window_s
    since_close = max(math.ceil(now - snapshot.closed_at), 1)
    return min(policy.window_s, since_close)


def tripped(
    policy: CircuitPolicy, snapshot: CircuitSnapshot, stats: CallStats, now: float
) -> CircuitSnapshot | None:
    """CLOSED → OPEN when enough calls were seen and the failure rate is at the threshold."""
    if snapshot.state is not CircuitState.CLOSED:
        return None
    if stats.total < policy.min_calls or stats.total == 0:
        return None
    if stats.failures / stats.total < policy.failure_rate_threshold:
        return None
    return CircuitSnapshot(CircuitState.OPEN, opened_at=now, open_duration_s=policy.open_duration_s)


def half_opened(snapshot: CircuitSnapshot) -> CircuitSnapshot:
    return CircuitSnapshot(
        CircuitState.HALF_OPEN, snapshot.opened_at, snapshot.open_duration_s, snapshot.closed_at
    )


def probe_succeeded(now: float) -> CircuitSnapshot:
    return CircuitSnapshot(CircuitState.CLOSED, closed_at=now)


def probe_failed(policy: CircuitPolicy, snapshot: CircuitSnapshot, now: float) -> CircuitSnapshot:
    """Back to OPEN for twice as long as last time, up to ``max_open_duration_s``."""
    previous = snapshot.open_duration_s or policy.open_duration_s
    doubled = min(previous * 2, policy.max_open_duration_s)
    return CircuitSnapshot(CircuitState.OPEN, opened_at=now, open_duration_s=doubled)
