"""Runtime dependencies the hot path relies on without importing them itself."""

import importlib.util
import timeit

import pytest

pytestmark = pytest.mark.unit


def test_sniffio_is_installed_so_httpcore_does_not_pay_a_failed_import_per_call() -> None:
    # httpcore.current_async_library() runs `import sniffio` inside try/except on every pool
    # operation. Missing, each call is a full module search (about 0.1 ms); present, a dict lookup.
    assert importlib.util.find_spec("sniffio") is not None


def test_httpcore_async_library_lookup_is_cheap() -> None:
    import asyncio

    from httpcore._synchronization import current_async_library

    async def measure() -> float:
        return timeit.timeit(current_async_library, number=2000) / 2000

    per_call_us = asyncio.run(measure()) * 1e6
    assert per_call_us < 20, f"{per_call_us:.1f} us per call"
