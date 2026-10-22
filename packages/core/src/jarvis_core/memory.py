"""In-memory implementations of the core interfaces.

Used by tests and as the fail-open fallback when Redis is unavailable (SPEC D3, §16.2).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from jarvis_core.clock import Clock
from jarvis_core.config import ApiDependency
from jarvis_core.interfaces import (
    CacheEntry,
    CacheLookup,
    CallStats,
    CircuitSnapshot,
    DedupRole,
    LLMCapabilities,
    LLMEvent,
    LLMMessage,
    RateDecision,
    SealedSecret,
    ToolSchema,
)


class InMemoryCacheStore:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._items: dict[str, tuple[CacheEntry, float, float]] = {}  # entry, fresh_until, gone_at

    async def get(self, key: str) -> CacheLookup | None:
        item = self._items.get(key)
        if item is None:
            return None
        entry, fresh_until, gone_at = item
        now = self._clock.now()
        if now >= gone_at:
            del self._items[key]
            return None
        return CacheLookup(entry=entry, fresh=now < fresh_until)

    async def set(self, key: str, entry: CacheEntry, *, ttl_s: int, stale_ttl_s: int) -> None:
        now = self._clock.now()
        self._items[key] = (entry, now + ttl_s, now + ttl_s + stale_ttl_s)

    async def delete(self, key: str) -> bool:
        return self._items.pop(key, None) is not None

    async def invalidate(self, prefix: str) -> int:
        doomed = [k for k in self._items if k.startswith(prefix)]
        for key in doomed:
            del self._items[key]
        return len(doomed)


class InMemoryRateLimiter:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # tokens, last refill (monotonic)

    async def take(
        self, key: str, *, capacity: int, refill_per_s: float, cost: int = 1
    ) -> RateDecision:
        now = self._clock.monotonic()
        tokens, last = self._buckets.get(key, (float(capacity), now))
        tokens = min(float(capacity), tokens + (now - last) * refill_per_s)
        if cost > capacity:
            self._buckets[key] = (tokens, now)
            return RateDecision(allowed=False, remaining=tokens, retry_after_s=None)
        if tokens >= cost:
            tokens -= cost
            self._buckets[key] = (tokens, now)
            return RateDecision(allowed=True, remaining=tokens, retry_after_s=None)
        self._buckets[key] = (tokens, now)
        return RateDecision(
            allowed=False, remaining=tokens, retry_after_s=(cost - tokens) / refill_per_s
        )


class InMemoryCircuitStore:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._snapshots: dict[str, CircuitSnapshot] = {}
        self._calls: dict[str, deque[tuple[float, bool]]] = defaultdict(deque)
        self._probes: dict[str, float] = {}

    async def get(self, api_id: str) -> CircuitSnapshot:
        return self._snapshots.get(api_id, CircuitSnapshot())

    async def set(self, api_id: str, snapshot: CircuitSnapshot) -> None:
        self._snapshots[api_id] = snapshot

    async def record_call(self, api_id: str, *, success: bool, window_s: int) -> CallStats:
        now = self._clock.now()
        calls = self._calls[api_id]
        calls.append((now, success))
        while calls and calls[0][0] <= now - window_s:
            calls.popleft()
        return CallStats(total=len(calls), failures=sum(1 for _, ok in calls if not ok))

    async def try_acquire_probe(self, api_id: str, *, ttl_s: int) -> bool:
        now = self._clock.now()
        if self._probes.get(api_id, 0.0) > now:
            return False
        self._probes[api_id] = now + ttl_s
        return True

    async def release_probe(self, api_id: str) -> None:
        self._probes.pop(api_id, None)


class InMemoryDeduplicator:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._locks: dict[str, float] = {}  # key -> expires_at
        self._events: dict[str, asyncio.Event] = {}

    def _live(self, key: str) -> bool:
        expires = self._locks.get(key)
        if expires is None:
            return False
        if self._clock.now() >= expires:
            self._free(key)
            return False
        return True

    def _free(self, key: str) -> None:
        self._locks.pop(key, None)
        event = self._events.pop(key, None)
        if event is not None:
            event.set()

    async def acquire(self, key: str, *, ttl_ms: int) -> DedupRole:
        if self._live(key):
            return DedupRole.FOLLOWER
        self._locks[key] = self._clock.now() + ttl_ms / 1000
        self._events[key] = asyncio.Event()
        return DedupRole.LEADER

    async def release(self, key: str) -> None:
        self._free(key)

    async def wait(self, key: str, *, timeout_s: float) -> bool:
        if not self._live(key):
            return True
        try:
            await asyncio.wait_for(self._events[key].wait(), timeout=timeout_s)
        except TimeoutError:
            return False
        return True


class InMemoryTelemetrySink:
    def __init__(self, capacity: int = 10_000) -> None:
        self._capacity = capacity
        self.events: list[Mapping[str, Any]] = []
        self._dropped = 0

    @property
    def dropped_total(self) -> int:
        return self._dropped

    def emit(self, event: Mapping[str, Any]) -> bool:
        if len(self.events) >= self._capacity:
            self._dropped += 1
            return False
        self.events.append(dict(event))
        return True


class InMemoryConfigSource:
    def __init__(self) -> None:
        self._versions: dict[tuple[str, str], list[ApiDependency]] = {}

    async def get(self, project_id: str, slug: str) -> ApiDependency | None:
        versions = self._versions.get((project_id, slug))
        return versions[-1] if versions else None

    async def list(self, project_id: str) -> list[ApiDependency]:
        return sorted(
            (v[-1] for (pid, _), v in self._versions.items() if pid == project_id),
            key=lambda api: api.slug,
        )

    async def put(self, project_id: str, api: ApiDependency) -> int:
        versions = self._versions.setdefault((project_id, api.slug), [])
        versions.append(api)
        return len(versions)


class InMemorySecretBox:
    """A reversible stand-in for tests and fail-open use.

    NOT cryptographically secure. Use ``jarvis_core.secretbox.AesGcmSecretBox`` for real
    AES-256-GCM encryption.
    """

    def __init__(self, key_id: str = "mem-1", key: bytes = b"jarvis-in-memory-key") -> None:
        self._keys = {key_id: key}
        self._key_id = key_id

    def _stream(self, key: bytes, nonce: bytes, length: int) -> bytes:
        out = b""
        counter = 0
        while len(out) < length:
            out += hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
            counter += 1
        return out[:length]

    def _tag(self, key: bytes, nonce: bytes, body: bytes) -> bytes:
        return hashlib.sha256(b"tag" + key + nonce + body).digest()[:8]

    def seal(self, plaintext: str) -> SealedSecret:
        key = self._keys[self._key_id]
        nonce = os.urandom(12)
        data = plaintext.encode()
        body = bytes(a ^ b for a, b in zip(data, self._stream(key, nonce, len(data)), strict=True))
        return SealedSecret(self._key_id, nonce, body + self._tag(key, nonce, body))

    def open(self, sealed: SealedSecret) -> str:
        key = self._keys[sealed.key_id]
        body, tag = sealed.ciphertext[:-8], sealed.ciphertext[-8:]
        if tag != self._tag(key, sealed.nonce, body):
            raise ValueError("secret failed authentication")
        stream = self._stream(key, sealed.nonce, len(body))
        return bytes(a ^ b for a, b in zip(body, stream, strict=True)).decode()


class ScriptedLLMProvider:
    """Replays a fixed script of events; for tests and the AI-off path."""

    def __init__(
        self,
        script: Sequence[LLMEvent] = (LLMEvent(type="text", text="ok"),),
        capabilities: LLMCapabilities | None = None,
    ) -> None:
        self._script = list(script)
        self._capabilities = capabilities or LLMCapabilities(
            tools=True, max_context=8192, json_mode=True
        )
        self.calls: list[tuple[Sequence[LLMMessage], Sequence[ToolSchema], bool]] = []

    @property
    def capabilities(self) -> LLMCapabilities:
        return self._capabilities

    async def chat(
        self, messages: Sequence[LLMMessage], tools: Sequence[ToolSchema], *, stream: bool
    ) -> AsyncIterator[LLMEvent]:
        self.calls.append((messages, tools, stream))
        for event in self._script:
            if event.type != "done":
                yield event
        yield LLMEvent(type="done")
