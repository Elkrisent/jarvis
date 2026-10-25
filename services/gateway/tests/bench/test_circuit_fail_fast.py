"""Fail-fast latency of an open circuit (metric ``circuit_open_fail_fast_p99``, tracked, M2.4).

The real gateway app is driven in process through ASGI with the circuit forced OPEN, so every
request is answered ``JV_CIRCUIT_OPEN`` without touching an upstream. The p99 of 3,000 sequential
requests is merged into ``$BENCH_DIR/results.json``. Run with ``make bench-circuit``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import httpx
import pytest
from benchlib import judge, load_budgets, read_json, write_json
from jarvis_core.clock import SystemClock
from jarvis_core.config import ApiDependency
from jarvis_core.interfaces import CircuitSnapshot, CircuitState
from jarvis_core.keys import hash_jarvis_key
from jarvis_core.memory import InMemoryCircuitStore
from jarvis_core.secretbox import AesGcmSecretBox
from jarvis_core.ssrf import SsrfPolicy
from jarvis_gateway.app import create_app
from jarvis_gateway.circuit import CircuitBreaker
from jarvis_gateway.config.manager import ConfigManager
from jarvis_gateway.config.snapshot import ApiEntry, KeyEntry, Snapshot
from jarvis_gateway.config.store import ConfigStore
from jarvis_gateway.credentials import CredentialVault
from jarvis_gateway.forwarder import Forwarder

pytestmark = pytest.mark.perf

ROOT = Path(__file__).resolve().parents[4]
METRIC = "circuit_open_fail_fast_p99"
KEY = "jv_live_benchcircuit001"
REQUESTS = 3000
WARMUP = 300


class StoreOnly:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


async def test_an_open_circuit_fails_fast() -> None:
    import time

    clock = SystemClock()
    circuits = InMemoryCircuitStore(clock)
    await circuits.set("api_1", CircuitSnapshot(CircuitState.OPEN, clock.now(), 3600))
    store = ConfigStore()
    config = ApiDependency.model_validate(
        {"slug": "weather", "name": "Weather", "base_url": "https://bench.example.com"}
    )
    keys = {hash_jarvis_key(KEY): KeyEntry("key_1", "prj_1", frozenset(), False)}
    store.replace(
        Snapshot.empty("postgres", 1.0).with_changes(
            apis={"api_1": ApiEntry("api_1", "prj_1", "weather", 1, config)}, keys=keys
        )
    )
    forwarder = Forwarder(
        SsrfPolicy(),
        CredentialVault(AesGcmSecretBox(bytes(range(32)))),
        max_body_bytes=1024 * 1024,
        circuit=CircuitBreaker(circuits, clock=clock),
    )
    app = create_app(config=cast(ConfigManager, StoreOnly(store)), forwarder=forwarder)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")
    samples: list[float] = []
    try:
        for n in range(WARMUP + REQUESTS):
            begin = time.perf_counter()
            response = await client.get("/gw/weather/v1/x", headers={"X-Jarvis-Key": KEY})
            elapsed = (time.perf_counter() - begin) * 1000
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "JV_CIRCUIT_OPEN"
            if n >= WARMUP:
                samples.append(elapsed)
    finally:
        await client.aclose()
        await forwarder.aclose()
    p99 = round(_percentile(samples, 99), 3)
    print(f"\ncircuit open fail-fast: p50={_percentile(samples, 50):.3f} ms p99={p99:.3f} ms")
    bench_dir = Path(os.environ.get("BENCH_DIR", "build/bench"))
    results_path = bench_dir / "results.json"
    results = read_json(results_path) if results_path.exists() else {}
    results[METRIC] = p99
    write_json(results_path, results)
    budget = load_budgets(ROOT / "benchmarks" / "budgets.yaml")[METRIC]
    assert judge(p99, budget) in ("ok", "no budget")  # tracked: no budget yet
