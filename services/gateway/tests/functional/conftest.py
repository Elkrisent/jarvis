"""Fixtures: the real gateway app, the real mock upstream, real sockets to the upstream.

The gateway is driven in-process through ASGI. Its upstream calls go over loopback TCP through the
real ``PinnedTransport``, with ``upstream.test`` mapped to 127.0.0.1 by a resolver stub.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import uvicorn
from jarvis_core.interfaces import TelemetrySink
from jarvis_core.keys import hash_jarvis_key
from jarvis_core.secretbox import AesGcmSecretBox
from jarvis_core.ssrf import SsrfPolicy
from jarvis_gateway.app import create_app
from jarvis_gateway.config.manager import ConfigManager
from jarvis_gateway.config.snapshot import ApiEntry, KeyEntry, Snapshot
from jarvis_gateway.config.store import ConfigStore
from jarvis_gateway.credentials import CredentialVault
from jarvis_gateway.forwarder import Forwarder
from jarvis_gateway.retry_budget import RetryBudget
from jarvis_gateway.upstream import PinnedTransport
from mock_upstream.app import create_app as create_mock_upstream

sys.path.insert(0, str(Path(__file__).parent))  # for the shared helpers in rig.py

from rig import (
    BOX,
    KEY,
    MAX_BODY,
    MODES,
    Gateway,
    GatewayFactory,
    RawServer,
    StoreOnly,
    Upstream,
    api_entry,
    free_port,
)


@pytest.fixture
async def upstream() -> AsyncIterator[Upstream]:
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_mock_upstream(seed=1),
            host="127.0.0.1",
            port=port,
            log_level="critical",
            timeout_graceful_shutdown=1,
        )
    )
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "mock upstream did not start"
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5) as admin:
        try:
            yield Upstream(port, admin)
        finally:
            server.should_exit = True
            await task


@pytest.fixture
async def make_gateway(upstream: Upstream) -> AsyncIterator[GatewayFactory]:
    created: list[Gateway] = []

    def build(
        *,
        port: int | None = None,
        box: AesGcmSecretBox = BOX,
        vault_box: AesGcmSecretBox = BOX,
        max_body_bytes: int = MAX_BODY,
        apis: Sequence[ApiEntry] | None = None,
        sink: TelemetrySink | None = None,
        retry_budget: RetryBudget | None = None,
        **cfg: Any,
    ) -> Gateway:
        target = port or upstream.port
        entries = (
            list(apis)
            if apis is not None
            else [
                api_entry(f"{name}-api", name, spec, target, box, **cfg)
                for name, spec in MODES.items()
            ]
        )
        store = ConfigStore()
        vault = CredentialVault(vault_box)
        store.add_listener(vault.refresh)
        keys = {hash_jarvis_key(KEY): KeyEntry("k1", "p1", frozenset(), False)}
        store.replace(
            Snapshot.empty("postgres", 1.0).with_changes(
                apis={e.api_id: e for e in entries}, keys=keys
            )
        )

        async def resolve(host: str, _port: int) -> Sequence[str]:
            return ["127.0.0.1"] if host == "upstream.test" else []

        policy = SsrfPolicy.from_env({"JARVIS_SSRF_ALLOWLIST": "upstream.test, 127.0.0.1/32"})
        forwarder = Forwarder(
            policy,
            vault,
            max_body_bytes=max_body_bytes,
            retry_budget=retry_budget,
            transport_factory=lambda: PinnedTransport(policy, resolver=resolve),
        )
        store.add_listener(forwarder.retain)
        app = create_app(
            config=cast(ConfigManager, StoreOnly(store)), forwarder=forwarder, sink=sink
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway", timeout=10
        )
        gateway = Gateway(client, store, forwarder, app)
        created.append(gateway)
        return gateway

    yield build
    for gateway in created:
        await gateway.client.aclose()
        await gateway.forwarder.aclose()


@pytest.fixture
async def raw_server() -> AsyncIterator[RawServer]:
    srv = RawServer()
    listener = await asyncio.start_server(srv.handle, "127.0.0.1", 0)
    srv.port = listener.sockets[0].getsockname()[1]
    try:
        yield srv
    finally:
        listener.close()
        await listener.wait_closed()
