"""The retry decision (SPEC §4.7): what is retryable, how long to wait, when to stop.

Pure functions over the policy, the failed attempt and the deadline. The retry *budget* (a Redis
counter across requests) is a separate concern (M2.3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Literal

from jarvis_core.clock import Random
from jarvis_core.config import RetryPolicy
from jarvis_core.deadline import Deadline

RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
RETRY_AFTER_STATUSES = frozenset({429, 503})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

_DELTA_SECONDS = re.compile(r"[0-9]{1,9}")


@dataclass(frozen=True)
class Failure:
    """What one attempt produced. ``connect`` is a connection error, ``timeout`` the attempt
    timeout; ``status`` carries the upstream's status and its raw ``Retry-After`` value."""

    kind: Literal["status", "connect", "timeout"]
    status: int | None = None
    retry_after: str | None = None


@dataclass(frozen=True)
class Decision:
    retry: bool
    delay_s: float = 0.0


NO_RETRY = Decision(False)


def method_retryable(method: str, idempotency_header: str | None) -> bool:
    """GET/HEAD/OPTIONS/PUT/DELETE always; anything else only with an idempotency header."""
    return method.upper() in IDEMPOTENT_METHODS or idempotency_header is not None


def condition_retryable(failure: Failure) -> bool:
    if failure.kind == "status":
        return failure.status in RETRYABLE_STATUSES
    return True


def parse_retry_after(value: str | None, now_epoch: float) -> float | None:
    """Seconds to wait from a ``Retry-After`` value (delta-seconds or HTTP-date); None if absent
    or malformed. A date in the past is 0."""
    if value is None:
        return None
    text = value.strip()
    if _DELTA_SECONDS.fullmatch(text):
        return float(int(text))
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return float(max(when.timestamp() - now_epoch, 0.0))


def backoff_ceiling_s(policy: RetryPolicy, retry_index: int) -> float:
    """``min(cap, base * 2**retry_index)`` in seconds; ``retry_index`` is 0 for the first retry."""
    exponent = min(max(retry_index, 0), 32)  # beyond this the cap has long won; avoids huge ints
    ceiling_ms: int = min(policy.cap_ms, policy.base_ms * 2**exponent)
    return ceiling_ms / 1000


def backoff_delay_s(policy: RetryPolicy, retry_index: int, rng: Random) -> float:
    """Full jitter: uniform over ``[0, ceiling]``."""
    return rng.uniform(0.0, backoff_ceiling_s(policy, retry_index))


def decide(
    policy: RetryPolicy,
    *,
    method: str,
    failure: Failure,
    attempts_made: int,
    deadline: Deadline,
    min_attempt_ms: int,
    rng: Random,
    now_epoch: float,
) -> Decision:
    """Retry or not, and after how long. ``attempts_made`` counts the attempt that just failed."""
    if not policy.enabled or attempts_made >= policy.max_attempts:
        return NO_RETRY
    if not method_retryable(method, policy.idempotency_header):
        return NO_RETRY
    if not condition_retryable(failure):
        return NO_RETRY
    delay = None
    if failure.kind == "status" and failure.status in RETRY_AFTER_STATUSES:
        delay = parse_retry_after(failure.retry_after, now_epoch)
    if delay is None:
        delay = backoff_delay_s(policy, attempts_made - 1, rng)
    if not deadline.can_start_attempt_after(delay, min_attempt_ms):
        return NO_RETRY  # also covers a Retry-After that does not fit the remaining deadline
    return Decision(True, delay)
