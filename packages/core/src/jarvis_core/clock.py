"""Injected time and randomness (IMPLEMENTATION A.2.6, A.3)."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Wall-clock time as epoch seconds."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds, for measuring durations."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class Random(Protocol):
    def random(self) -> float:
        """A float in [0.0, 1.0)."""
        ...

    def uniform(self, a: float, b: float) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SystemRandom:
    def __init__(self) -> None:
        self._rng = random.Random()

    def random(self) -> float:
        return self._rng.random()

    def uniform(self, a: float, b: float) -> float:
        return self._rng.uniform(a, b)


class FakeClock:
    """Deterministic clock. ``sleep`` advances time instead of waiting."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start
        self._mono = 0.0

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move a clock backwards")
        self._now += seconds
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(max(seconds, 0.0))
        await asyncio.sleep(0)


class FakeRandom:
    """Seeded randomness, or a fixed sequence of values for exact assertions."""

    def __init__(self, seed: int = 0, sequence: list[float] | None = None) -> None:
        self._rng = random.Random(seed)
        self._sequence = list(sequence) if sequence is not None else None
        self._index = 0

    def random(self) -> float:
        if self._sequence is None:
            return self._rng.random()
        value = self._sequence[self._index % len(self._sequence)]
        self._index += 1
        return value

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.random()
