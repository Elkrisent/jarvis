import pytest
from jarvis_core.clock import FakeClock, FakeRandom, SystemClock, SystemRandom

pytestmark = pytest.mark.unit


def test_fake_clock_advances_both_clocks() -> None:
    clock = FakeClock(start=100.0)
    clock.advance(2.5)
    assert clock.now() == 102.5
    assert clock.monotonic() == 2.5


def test_fake_clock_cannot_go_backwards() -> None:
    with pytest.raises(ValueError, match="backwards"):
        FakeClock().advance(-1)


async def test_fake_clock_sleep_advances_without_waiting() -> None:
    clock = FakeClock()
    await clock.sleep(3600)
    assert clock.monotonic() == 3600


async def test_fake_clock_negative_sleep_is_a_noop() -> None:
    clock = FakeClock()
    await clock.sleep(-5)
    assert clock.monotonic() == 0


def test_fake_random_is_deterministic_per_seed() -> None:
    a, b = FakeRandom(1), FakeRandom(1)
    assert [a.random() for _ in range(5)] == [b.random() for _ in range(5)]


def test_fake_random_sequence_cycles() -> None:
    rng = FakeRandom(sequence=[0.0, 0.5])
    assert [rng.random() for _ in range(5)] == [0.0, 0.5, 0.0, 0.5, 0.0]


def test_fake_random_uniform_scales_the_sequence() -> None:
    assert FakeRandom(sequence=[0.5]).uniform(10, 20) == 15


async def test_system_implementations_work() -> None:
    clock = SystemClock()
    before = clock.monotonic()
    await clock.sleep(0)
    assert clock.monotonic() >= before
    assert clock.now() > 0
    rng = SystemRandom()
    assert 0.0 <= rng.random() < 1.0
    assert 2.0 <= rng.uniform(2.0, 3.0) <= 3.0
