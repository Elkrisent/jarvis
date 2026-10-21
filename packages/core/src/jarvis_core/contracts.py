"""Reusable contract suites, one per interface.

Subclass a suite, override its ``make_*`` fixture, and add the test-type marker
(``@pytest.mark.unit`` or ``@pytest.mark.integration``). Every implementation, in-memory,
Redis or Postgres, must pass the same suite.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from jarvis_core.clock import FakeClock
from jarvis_core.config import ApiDependency
from jarvis_core.interfaces import (
    CacheEntry,
    CacheStore,
    CircuitSnapshot,
    CircuitState,
    CircuitStore,
    ConfigSource,
    Deduplicator,
    DedupRole,
    LLMEvent,
    LLMMessage,
    LLMProvider,
    RateLimiter,
    SecretBox,
    TelemetrySink,
)


def _entry(body: bytes = b"hello", stored_at: float = 0.0) -> CacheEntry:
    return CacheEntry(
        status=200, headers=(("content-type", "text/plain"),), body=body, stored_at=stored_at
    )


# Implementations that need the fake clock override ``advance``: the in-memory ones share
# the ``clock`` fixture; a Redis implementation would use short real TTLs instead.
class CacheStoreContract:
    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def store(self, clock: FakeClock) -> CacheStore:
        raise NotImplementedError

    async def test_miss_returns_none(self, store: CacheStore) -> None:
        assert await store.get("cache:p:a:missing") is None

    async def test_set_then_get_is_fresh(self, store: CacheStore) -> None:
        await store.set("k", _entry(), ttl_s=60, stale_ttl_s=600)
        found = await store.get("k")
        assert found is not None
        assert found.fresh
        assert found.entry.body == b"hello"

    @pytest.mark.req("REQ-4.9-7")
    async def test_becomes_stale_after_ttl(self, store: CacheStore, clock: FakeClock) -> None:
        await store.set("k", _entry(), ttl_s=60, stale_ttl_s=600)
        clock.advance(61)
        found = await store.get("k")
        assert found is not None
        assert not found.fresh

    async def test_gone_after_stale_window(self, store: CacheStore, clock: FakeClock) -> None:
        await store.set("k", _entry(), ttl_s=60, stale_ttl_s=600)
        clock.advance(661)
        assert await store.get("k") is None

    async def test_set_overwrites(self, store: CacheStore) -> None:
        await store.set("k", _entry(b"one"), ttl_s=60, stale_ttl_s=0)
        await store.set("k", _entry(b"two"), ttl_s=60, stale_ttl_s=0)
        found = await store.get("k")
        assert found is not None
        assert found.entry.body == b"two"

    async def test_delete(self, store: CacheStore) -> None:
        await store.set("k", _entry(), ttl_s=60, stale_ttl_s=0)
        assert await store.delete("k") is True
        assert await store.delete("k") is False
        assert await store.get("k") is None

    @pytest.mark.req("REQ-4.9-8")
    async def test_invalidate_by_prefix_only_touches_matching_keys(self, store: CacheStore) -> None:
        for key in ("cache:p:a:1", "cache:p:a:2", "cache:p:b:1"):
            await store.set(key, _entry(), ttl_s=60, stale_ttl_s=0)
        assert await store.invalidate("cache:p:a:") == 2
        assert await store.get("cache:p:a:1") is None
        assert await store.get("cache:p:b:1") is not None

    async def test_invalidate_empty_prefix_clears_everything(self, store: CacheStore) -> None:
        for key in ("x", "y"):
            await store.set(key, _entry(), ttl_s=60, stale_ttl_s=0)
        assert await store.invalidate("") == 2


class RateLimiterContract:
    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def limiter(self, clock: FakeClock) -> RateLimiter:
        raise NotImplementedError

    @pytest.mark.req("REQ-4.11-5")
    async def test_bucket_starts_full(self, limiter: RateLimiter) -> None:
        for _ in range(3):
            assert (await limiter.take("k", capacity=3, refill_per_s=1)).allowed
        assert not (await limiter.take("k", capacity=3, refill_per_s=1)).allowed

    async def test_reject_reports_retry_after(self, limiter: RateLimiter) -> None:
        await limiter.take("k", capacity=1, refill_per_s=2)
        decision = await limiter.take("k", capacity=1, refill_per_s=2)
        assert not decision.allowed
        assert decision.retry_after_s == pytest.approx(0.5, abs=0.05)

    async def test_refills_over_time(self, limiter: RateLimiter, clock: FakeClock) -> None:
        await limiter.take("k", capacity=1, refill_per_s=1)
        assert not (await limiter.take("k", capacity=1, refill_per_s=1)).allowed
        clock.advance(1.1)
        assert (await limiter.take("k", capacity=1, refill_per_s=1)).allowed

    @pytest.mark.req("REQ-4.11-5")
    async def test_refill_never_exceeds_capacity(
        self, limiter: RateLimiter, clock: FakeClock
    ) -> None:
        await limiter.take("k", capacity=2, refill_per_s=10)
        clock.advance(3600)
        assert (await limiter.take("k", capacity=2, refill_per_s=10)).allowed
        assert (await limiter.take("k", capacity=2, refill_per_s=10)).allowed
        assert not (await limiter.take("k", capacity=2, refill_per_s=10)).allowed

    async def test_keys_are_independent(self, limiter: RateLimiter) -> None:
        await limiter.take("a", capacity=1, refill_per_s=1)
        assert (await limiter.take("b", capacity=1, refill_per_s=1)).allowed

    async def test_cost_above_capacity_never_allowed(self, limiter: RateLimiter) -> None:
        decision = await limiter.take("k", capacity=2, refill_per_s=1, cost=3)
        assert not decision.allowed
        assert decision.retry_after_s is None

    @pytest.mark.req("REQ-4.11-6")
    async def test_rejected_take_does_not_consume_tokens(self, limiter: RateLimiter) -> None:
        await limiter.take("k", capacity=2, refill_per_s=1, cost=1)
        assert not (await limiter.take("k", capacity=2, refill_per_s=1, cost=2)).allowed
        assert (await limiter.take("k", capacity=2, refill_per_s=1, cost=1)).allowed


class CircuitStoreContract:
    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def store(self, clock: FakeClock) -> CircuitStore:
        raise NotImplementedError

    async def test_default_state_is_closed(self, store: CircuitStore) -> None:
        assert (await store.get("api")).state is CircuitState.CLOSED

    async def test_set_then_get(self, store: CircuitStore) -> None:
        snap = CircuitSnapshot(CircuitState.OPEN, opened_at=100.0, open_duration_s=30)
        await store.set("api", snap)
        assert await store.get("api") == snap
        assert (await store.get("other")).state is CircuitState.CLOSED

    async def test_record_call_counts_within_window(self, store: CircuitStore) -> None:
        await store.record_call("api", success=True, window_s=30)
        await store.record_call("api", success=False, window_s=30)
        stats = await store.record_call("api", success=False, window_s=30)
        assert (stats.total, stats.failures) == (3, 2)

    async def test_calls_age_out_of_window(self, store: CircuitStore, clock: FakeClock) -> None:
        await store.record_call("api", success=False, window_s=30)
        clock.advance(31)
        stats = await store.record_call("api", success=True, window_s=30)
        assert (stats.total, stats.failures) == (1, 0)

    async def test_apis_are_independent(self, store: CircuitStore) -> None:
        await store.record_call("a", success=False, window_s=30)
        stats = await store.record_call("b", success=True, window_s=30)
        assert stats.total == 1

    @pytest.mark.req("REQ-4.8-6")
    async def test_only_one_probe_wins(self, store: CircuitStore) -> None:
        results = await asyncio.gather(
            *(store.try_acquire_probe("api", ttl_s=30) for _ in range(50))
        )
        assert results.count(True) == 1

    async def test_probe_can_be_reacquired_after_release(self, store: CircuitStore) -> None:
        assert await store.try_acquire_probe("api", ttl_s=30)
        assert not await store.try_acquire_probe("api", ttl_s=30)
        await store.release_probe("api")
        assert await store.try_acquire_probe("api", ttl_s=30)

    async def test_probe_lock_expires(self, store: CircuitStore, clock: FakeClock) -> None:
        assert await store.try_acquire_probe("api", ttl_s=30)
        clock.advance(31)
        assert await store.try_acquire_probe("api", ttl_s=30)


class DeduplicatorContract:
    @pytest.fixture
    def clock(self) -> FakeClock:
        return FakeClock()

    @pytest.fixture
    def dedup(self, clock: FakeClock) -> Deduplicator:
        raise NotImplementedError

    async def test_first_is_leader_rest_are_followers(self, dedup: Deduplicator) -> None:
        roles = [await dedup.acquire("k", ttl_ms=5000) for _ in range(3)]
        assert roles == [DedupRole.LEADER, DedupRole.FOLLOWER, DedupRole.FOLLOWER]

    @pytest.mark.req("REQ-4.10-3")
    async def test_exactly_one_leader_under_concurrency(self, dedup: Deduplicator) -> None:
        roles = await asyncio.gather(*(dedup.acquire("k", ttl_ms=5000) for _ in range(100)))
        assert roles.count(DedupRole.LEADER) == 1

    async def test_keys_are_independent(self, dedup: Deduplicator) -> None:
        assert await dedup.acquire("a", ttl_ms=5000) is DedupRole.LEADER
        assert await dedup.acquire("b", ttl_ms=5000) is DedupRole.LEADER

    async def test_release_wakes_followers(self, dedup: Deduplicator) -> None:
        await dedup.acquire("k", ttl_ms=5000)
        waiter = asyncio.create_task(dedup.wait("k", timeout_s=5))
        await asyncio.sleep(0)
        await dedup.release("k")
        assert await waiter is True

    async def test_wait_times_out_while_leader_holds(self, dedup: Deduplicator) -> None:
        await dedup.acquire("k", ttl_ms=5000)
        assert await dedup.wait("k", timeout_s=0) is False

    async def test_wait_returns_immediately_when_nobody_leads(self, dedup: Deduplicator) -> None:
        assert await dedup.wait("k", timeout_s=0) is True

    async def test_release_allows_a_new_leader(self, dedup: Deduplicator) -> None:
        await dedup.acquire("k", ttl_ms=5000)
        await dedup.release("k")
        assert await dedup.acquire("k", ttl_ms=5000) is DedupRole.LEADER

    @pytest.mark.req("REQ-4.10-4")
    async def test_dead_leader_lock_expires(self, dedup: Deduplicator, clock: FakeClock) -> None:
        await dedup.acquire("k", ttl_ms=1000)
        clock.advance(1.5)
        assert await dedup.acquire("k", ttl_ms=1000) is DedupRole.LEADER


class TelemetrySinkContract:
    """Override ``make_sink`` to build a sink holding at most ``capacity`` queued events."""

    @pytest.fixture
    def make_sink(self) -> Callable[[int], TelemetrySink]:
        raise NotImplementedError

    @pytest.fixture
    def read_events(self) -> Callable[[TelemetrySink], Awaitable[list[Any]]]:
        raise NotImplementedError

    async def test_emit_accepts_events(self, make_sink: Callable[[int], TelemetrySink]) -> None:
        sink = make_sink(10)
        assert sink.emit({"request_id": "1"}) is True
        assert sink.dropped_total == 0

    @pytest.mark.req("REQ-6.3-2")
    async def test_full_queue_drops_and_counts(
        self, make_sink: Callable[[int], TelemetrySink]
    ) -> None:
        sink = make_sink(2)
        results = [sink.emit({"n": n}) for n in range(5)]
        assert results == [True, True, False, False, False]
        assert sink.dropped_total == 3

    @pytest.mark.req("REQ-6.3-1")
    async def test_emit_does_not_block(self, make_sink: Callable[[int], TelemetrySink]) -> None:
        sink = make_sink(1)
        sink.emit({"n": 0})
        # a full queue must return immediately rather than await space
        assert sink.emit({"n": 1}) is False

    async def test_emitted_events_are_readable_in_order(
        self,
        make_sink: Callable[[int], TelemetrySink],
        read_events: Callable[[TelemetrySink], Awaitable[list[Any]]],
    ) -> None:
        sink = make_sink(10)
        for n in range(3):
            sink.emit({"n": n})
        assert [e["n"] for e in await read_events(sink)] == [0, 1, 2]


class ConfigSourceContract:
    @pytest.fixture
    def source(self) -> ConfigSource:
        raise NotImplementedError

    @staticmethod
    def _api(slug: str = "weather", name: str = "Weather") -> ApiDependency:
        return ApiDependency(slug=slug, name=name, base_url="https://api.weather.example")

    async def test_missing_returns_none(self, source: ConfigSource) -> None:
        assert await source.get("prj_1", "weather") is None

    async def test_put_then_get(self, source: ConfigSource) -> None:
        await source.put("prj_1", self._api())
        assert await source.get("prj_1", "weather") == self._api()

    async def test_put_returns_increasing_versions_and_get_returns_latest(
        self, source: ConfigSource
    ) -> None:
        assert await source.put("prj_1", self._api(name="v1")) == 1
        assert await source.put("prj_1", self._api(name="v2")) == 2
        latest = await source.get("prj_1", "weather")
        assert latest is not None
        assert latest.name == "v2"

    async def test_list_is_scoped_to_project_and_sorted(self, source: ConfigSource) -> None:
        await source.put("prj_1", self._api("zeta"))
        await source.put("prj_1", self._api("alpha"))
        await source.put("prj_2", self._api("other"))
        assert [a.slug for a in await source.list("prj_1")] == ["alpha", "zeta"]

    async def test_projects_do_not_share_slugs(self, source: ConfigSource) -> None:
        await source.put("prj_1", self._api())
        assert await source.get("prj_2", "weather") is None


class SecretBoxContract:
    @pytest.fixture
    def box(self) -> SecretBox:
        raise NotImplementedError

    def test_round_trip(self, box: SecretBox) -> None:
        assert box.open(box.seal("s3cret-value")) == "s3cret-value"

    def test_round_trip_unicode_and_empty(self, box: SecretBox) -> None:
        for value in ("", "pässwörd-🔑"):
            assert box.open(box.seal(value)) == value

    def test_ciphertext_does_not_contain_plaintext(self, box: SecretBox) -> None:
        sealed = box.seal("s3cret-value")
        assert b"s3cret-value" not in sealed.ciphertext

    def test_nonce_is_unique_per_record(self, box: SecretBox) -> None:
        a, b = box.seal("same"), box.seal("same")
        assert a.nonce != b.nonce
        assert a.ciphertext != b.ciphertext

    def test_key_id_is_recorded(self, box: SecretBox) -> None:
        assert box.seal("x").key_id

    def test_tampering_is_detected(self, box: SecretBox) -> None:
        sealed = box.seal("s3cret-value")
        flipped = bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]
        with pytest.raises(ValueError, match="authentication"):
            box.open(type(sealed)(sealed.key_id, sealed.nonce, flipped))

    def test_unknown_key_id_is_rejected(self, box: SecretBox) -> None:
        sealed = box.seal("x")
        with pytest.raises(KeyError):
            box.open(type(sealed)("no-such-key", sealed.nonce, sealed.ciphertext))


class LLMProviderContract:
    @pytest.fixture
    def provider(self) -> LLMProvider:
        raise NotImplementedError

    async def _collect(self, provider: LLMProvider, *, stream: bool) -> list[LLMEvent]:
        messages = [LLMMessage(role="user", content="hello")]
        return [event async for event in provider.chat(messages, [], stream=stream)]

    @pytest.mark.req("REQ-9.2-1")
    def test_capabilities_are_sane(self, provider: LLMProvider) -> None:
        assert provider.capabilities.max_context > 0

    @pytest.mark.parametrize("stream", [True, False])
    @pytest.mark.req("REQ-9.2-1")
    async def test_stream_ends_with_exactly_one_done(
        self, provider: LLMProvider, stream: bool
    ) -> None:
        events = await self._collect(provider, stream=stream)
        assert [e.type for e in events].count("done") == 1
        assert events[-1].type == "done"

    async def test_tool_calls_carry_a_name(self, provider: LLMProvider) -> None:
        for event in await self._collect(provider, stream=True):
            if event.type == "tool_call":
                assert event.tool_name
