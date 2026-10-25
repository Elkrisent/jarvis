"""Shared helpers for the functional suite: constants, the upstream/gateway handles, a raw server.

The gateway is driven in-process through ASGI. Its upstream calls go over loopback TCP through the
real ``PinnedTransport``, with ``upstream.test`` mapped to 127.0.0.1 by a resolver stub.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn
from jarvis_core.config import ApiDependency
from jarvis_core.secretbox import AesGcmSecretBox
from jarvis_gateway.config.manager import ConfigStatus
from jarvis_gateway.config.snapshot import ApiEntry
from jarvis_gateway.config.store import ConfigStore
from jarvis_gateway.forwarder import Forwarder

BOX = AesGcmSecretBox(bytes(range(32)))
OTHER_BOX = AesGcmSecretBox(bytes(range(1, 33)))
SECRET = "sk-REAL-provider-secret-9"
KEY = "jv_live_functionalkey01"
KEY2 = "jv_live_functionalkey02"
BASE_PATH = "/base"
MAX_BODY = 64 * 1024
MODES = {
    "bearer": {"mode": "bearer"},
    "header": {"mode": "header", "header_name": "X-Api-Key"},
    "query": {"mode": "query", "param_name": "appid"},
    "none": {"mode": "none"},
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class StoreOnly:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def status(self) -> ConfigStatus:
        now = time.time()
        return ConfigStatus("postgres", 1, 1, now, now)


@dataclass
class Upstream:
    port: int
    admin: httpx.AsyncClient

    async def calls(self) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = (await self.admin.get("/__admin/calls")).json()["calls"]
        return calls

    async def rules(self, *rules: dict[str, Any]) -> None:
        response = await self.admin.post("/__admin/rules", json=list(rules))
        assert response.status_code == 200, response.text


@dataclass
class Gateway:
    client: httpx.AsyncClient
    store: ConfigStore
    forwarder: Forwarder
    app: Any

    async def call(
        self, method: str, url: str, *, key: str | None = KEY, **kw: Any
    ) -> httpx.Response:
        headers = dict(kw.pop("headers", {}))
        if key is not None:
            headers.setdefault("X-Jarvis-Key", key)
        return await self.client.request(method, url, headers=headers, **kw)


GatewayFactory = Callable[..., Gateway]


def api_entry(
    api_id: str,
    slug: str,
    credential: dict[str, Any],
    port: int,
    box: AesGcmSecretBox = BOX,
    **cfg: Any,
) -> ApiEntry:
    cred = dict(credential)
    if cred["mode"] != "none":
        cred["secret_ref"] = f"sec_{api_id}"
    config = ApiDependency.model_validate(
        {
            "slug": slug,
            "name": slug,
            "base_url": f"http://upstream.test:{port}{BASE_PATH}",
            "credential": cred,
            "jarvis_key_slot": "authorization",
            "deadline_ms": 1500,
            "attempt_timeout_ms": 1000,
            "min_attempt_ms": 100,
            **cfg,
        }
    )
    sealed = box.seal(SECRET, context=f"sec_{api_id}") if cred["mode"] != "none" else None
    return ApiEntry(api_id, "p1", slug, 1, config, sealed)


class RawServer:
    """A bare HTTP/1.1 server that records each request exactly as it arrived."""

    def __init__(self) -> None:
        self.heads: list[str] = []
        self.bodies: list[bytes] = []
        self.response: bytes = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
        self.port = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        length = 0
        for line in head.split("\r\n")[1:]:
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1])
        body = await reader.readexactly(length) if length else b""
        self.heads.append(head)
        self.bodies.append(body)
        writer.write(self.response)
        await writer.drain()
        writer.close()

    def header_lines(self, index: int = 0) -> list[str]:
        return self.heads[index].split("\r\n")[1:-2]

    @property
    def request_line(self) -> str:
        return self.heads[0].split("\r\n")[0]


@asynccontextmanager
async def serve(app: Any) -> AsyncIterator[int]:
    """Run an ASGI app under uvicorn on a free loopback port; yields the port."""
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="critical", timeout_graceful_shutdown=1
        )
    )
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "gateway did not start"
    try:
        yield port
    finally:
        server.should_exit = True
        await task


async def raw_exchange(port: int, request: bytes) -> tuple[str, list[str], bytes]:
    """Send raw bytes, read until the server closes; returns status line, header lines, body."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(-1), timeout=10)
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    return lines[0], lines[1:], body


def dechunk(data: bytes) -> bytes:
    """Decode an HTTP/1.1 chunked body."""
    out = b""
    while data:
        size_line, _, data = data.partition(b"\r\n")
        size = int(size_line.split(b";")[0], 16)
        if size == 0:
            break
        out += data[:size]
        data = data[size + 2 :]
    return out
