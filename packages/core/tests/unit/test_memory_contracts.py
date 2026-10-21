"""Run every contract suite against the in-memory implementations."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from jarvis_core.clock import FakeClock
from jarvis_core.contracts import (
    CacheStoreContract,
    CircuitStoreContract,
    ConfigSourceContract,
    DeduplicatorContract,
    LLMProviderContract,
    RateLimiterContract,
    SecretBoxContract,
    TelemetrySinkContract,
)
from jarvis_core.interfaces import (
    CacheStore,
    CircuitStore,
    ConfigSource,
    Deduplicator,
    LLMEvent,
    LLMProvider,
    RateLimiter,
    SecretBox,
    TelemetrySink,
)
from jarvis_core.memory import (
    InMemoryCacheStore,
    InMemoryCircuitStore,
    InMemoryConfigSource,
    InMemoryDeduplicator,
    InMemoryRateLimiter,
    InMemorySecretBox,
    InMemoryTelemetrySink,
    ScriptedLLMProvider,
)


@pytest.mark.unit
@pytest.mark.req("REQ-16.2-2")
class TestInMemoryCacheStore(CacheStoreContract):
    @pytest.fixture
    def store(self, clock: FakeClock) -> CacheStore:
        return InMemoryCacheStore(clock)


@pytest.mark.unit
@pytest.mark.req("REQ-16.2-2")
class TestInMemoryRateLimiter(RateLimiterContract):
    @pytest.fixture
    def limiter(self, clock: FakeClock) -> RateLimiter:
        return InMemoryRateLimiter(clock)


@pytest.mark.unit
@pytest.mark.req("REQ-16.2-2")
class TestInMemoryCircuitStore(CircuitStoreContract):
    @pytest.fixture
    def store(self, clock: FakeClock) -> CircuitStore:
        return InMemoryCircuitStore(clock)


@pytest.mark.unit
@pytest.mark.req("REQ-16.2-2")
class TestInMemoryDeduplicator(DeduplicatorContract):
    @pytest.fixture
    def dedup(self, clock: FakeClock) -> Deduplicator:
        return InMemoryDeduplicator(clock)


@pytest.mark.unit
@pytest.mark.req("REQ-16.2-2")
class TestInMemoryTelemetrySink(TelemetrySinkContract):
    @pytest.fixture
    def make_sink(self) -> Callable[[int], TelemetrySink]:
        return InMemoryTelemetrySink

    @pytest.fixture
    def read_events(self) -> Callable[[TelemetrySink], Awaitable[list[Any]]]:
        async def read(sink: TelemetrySink) -> list[Any]:
            assert isinstance(sink, InMemoryTelemetrySink)
            return list(sink.events)

        return read


@pytest.mark.unit
@pytest.mark.req("REQ-16.2-2")
class TestInMemoryConfigSource(ConfigSourceContract):
    @pytest.fixture
    def source(self) -> ConfigSource:
        return InMemoryConfigSource()


@pytest.mark.unit
@pytest.mark.req("REQ-16.2-2")
class TestInMemorySecretBox(SecretBoxContract):
    @pytest.fixture
    def box(self) -> SecretBox:
        return InMemorySecretBox()


@pytest.mark.unit
@pytest.mark.req("REQ-16.2-2")
class TestScriptedLLMProvider(LLMProviderContract):
    @pytest.fixture
    def provider(self) -> LLMProvider:
        return ScriptedLLMProvider(
            [
                LLMEvent(type="text", text="thinking"),
                LLMEvent(type="tool_call", tool_name="list_apis"),
                LLMEvent(type="done"),
            ]
        )


@pytest.mark.unit
async def test_scripted_provider_records_calls_and_always_ends_with_one_done() -> None:
    provider = ScriptedLLMProvider([LLMEvent(type="done"), LLMEvent(type="text", text="a")])
    events = [e async for e in provider.chat([], [], stream=False)]
    assert [e.type for e in events] == ["text", "done"]
    assert len(provider.calls) == 1


@pytest.mark.unit
@pytest.mark.req("REQ-16.2-1")
def test_core_exposes_every_spec_interface() -> None:
    from jarvis_core import interfaces

    for name in (
        "CacheStore",
        "RateLimiter",
        "CircuitStore",
        "Deduplicator",
        "TelemetrySink",
        "ConfigSource",
        "SecretBox",
        "LLMProvider",
    ):
        assert hasattr(interfaces, name)
