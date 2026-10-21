"""Core interfaces (SPEC §16.2, IMPLEMENTATION M0.2).

Every interface has an in-memory implementation in ``jarvis_core.memory`` and a reusable
contract suite in ``jarvis_core.contracts``. Redis/Postgres implementations must pass the
same suites.
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from jarvis_core.config import ApiDependency

# ---------------------------------------------------------------- CacheStore


@dataclass(frozen=True)
class CacheEntry:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    stored_at: float


@dataclass(frozen=True)
class CacheLookup:
    entry: CacheEntry
    fresh: bool  # False = stale: usable only as a fallback (SPEC §4.9)


class CacheStore(Protocol):
    async def get(self, key: str) -> CacheLookup | None:
        """Fresh until ``ttl_s`` after storing, stale for ``stale_ttl_s`` more, then gone."""
        ...

    async def set(self, key: str, entry: CacheEntry, *, ttl_s: int, stale_ttl_s: int) -> None: ...

    async def delete(self, key: str) -> bool: ...

    async def invalidate(self, prefix: str) -> int:
        """Delete every key starting with ``prefix`` ('' = everything). Returns the count."""
        ...


# --------------------------------------------------------------- RateLimiter


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    remaining: float
    retry_after_s: float | None  # None when allowed, or when the cost can never be met


class RateLimiter(Protocol):
    async def take(
        self, key: str, *, capacity: int, refill_per_s: float, cost: int = 1
    ) -> RateDecision:
        """Token bucket (SPEC §4.11). A new bucket starts full."""
        ...


# ------------------------------------------------------------- CircuitStore


class CircuitState(enum.StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True)
class CircuitSnapshot:
    state: CircuitState = CircuitState.CLOSED
    opened_at: float | None = None
    open_duration_s: int = 0


@dataclass(frozen=True)
class CallStats:
    total: int
    failures: int


class CircuitStore(Protocol):
    """Shared circuit state. The transition logic itself is a pure state machine (M2.4)."""

    async def get(self, api_id: str) -> CircuitSnapshot: ...

    async def set(self, api_id: str, snapshot: CircuitSnapshot) -> None: ...

    async def record_call(self, api_id: str, *, success: bool, window_s: int) -> CallStats:
        """Record one outcome and return totals over the last ``window_s`` seconds."""
        ...

    async def try_acquire_probe(self, api_id: str, *, ttl_s: int) -> bool:
        """Take the single half-open probe slot. Exactly one concurrent caller wins."""
        ...

    async def release_probe(self, api_id: str) -> None: ...


# ------------------------------------------------------------- Deduplicator


class DedupRole(enum.StrEnum):
    LEADER = "leader"
    FOLLOWER = "follower"


class Deduplicator(Protocol):
    async def acquire(self, key: str, *, ttl_ms: int) -> DedupRole:
        """First caller becomes leader; the lock expires after ``ttl_ms`` if never released."""
        ...

    async def release(self, key: str) -> None:
        """Leader is done: free the key and wake followers."""
        ...

    async def wait(self, key: str, *, timeout_s: float) -> bool:
        """Follower waits for the leader. False on timeout. True once the key is free."""
        ...


# ----------------------------------------------------------- TelemetrySink


class TelemetrySink(Protocol):
    @property
    def dropped_total(self) -> int: ...

    def emit(self, event: Mapping[str, Any]) -> bool:
        """Never blocks. False (and counted) if the event was dropped (SPEC §6.3)."""
        ...


# ------------------------------------------------------------- ConfigSource


class ConfigSource(Protocol):
    async def get(self, project_id: str, slug: str) -> ApiDependency | None: ...

    async def list(self, project_id: str) -> list[ApiDependency]: ...

    async def put(self, project_id: str, api: ApiDependency) -> int:
        """Store ``api`` as a new immutable version and return its version number (from 1)."""
        ...


# ---------------------------------------------------------------- SecretBox


@dataclass(frozen=True)
class SealedSecret:
    key_id: str
    nonce: bytes
    ciphertext: bytes


class SecretBox(Protocol):
    def seal(self, plaintext: str) -> SealedSecret: ...

    def open(self, sealed: SealedSecret) -> str:
        """Raises ``KeyError`` for an unknown ``key_id``, ``ValueError`` if tampered with."""
        ...


# -------------------------------------------------------------- LLMProvider


@dataclass(frozen=True)
class LLMMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_name: str | None = None


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMCapabilities:
    tools: bool
    max_context: int
    json_mode: bool


@dataclass(frozen=True)
class LLMEvent:
    """One normalized event. A stream always ends with exactly one ``done``."""

    type: Literal["text", "tool_call", "done"]
    text: str = ""
    tool_name: str | None = None
    tool_args: Mapping[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    @property
    def capabilities(self) -> LLMCapabilities: ...

    def chat(
        self, messages: Sequence[LLMMessage], tools: Sequence[ToolSchema], *, stream: bool
    ) -> AsyncIterator[LLMEvent]: ...
