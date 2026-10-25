"""The per-request time budget (SPEC §4.6), measured on the injected ``Clock``."""

from __future__ import annotations

from jarvis_core.clock import Clock


class Deadline:
    """``total_ms`` of budget starting at ``started`` (a ``clock.monotonic()`` reading)."""

    def __init__(self, clock: Clock, started: float, total_ms: int) -> None:
        if total_ms <= 0:
            raise ValueError("a deadline must be positive")
        self._clock = clock
        self._end = started + total_ms / 1000

    def remaining(self) -> float:
        """Seconds left, never negative."""
        return max(self._end - self._clock.monotonic(), 0.0)

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def attempt_timeout(self, attempt_timeout_s: float) -> float:
        """The limit for one attempt: the configured timeout, or what is left if that is less."""
        return min(attempt_timeout_s, self.remaining())

    def can_start_attempt(self, min_attempt_ms: int) -> bool:
        """A new attempt starts only if at least ``min_attempt_ms`` remains."""
        return self.can_start_attempt_after(0.0, min_attempt_ms)

    def can_start_attempt_after(self, wait_s: float, min_attempt_ms: int) -> bool:
        """Would at least ``min_attempt_ms`` still remain once ``wait_s`` has been waited?"""
        # Rounded to the microsecond so float noise (5.0 - 4.7 = 0.29999...) cannot flip the edge.
        return round((self.remaining() - wait_s) * 1000, 3) >= min_attempt_ms
