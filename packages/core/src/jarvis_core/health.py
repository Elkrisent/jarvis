"""Dependency readiness for ``/readyz`` (SPEC §8, §15.1).

``ok``: every dependency is reachable. ``degraded``: only optional ones are down, and the
service still works (HTTP 200). ``unavailable``: a required one is down (HTTP 503).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Dependency:
    name: str
    host: str
    port: int
    required: bool = True


@dataclass(frozen=True)
class Readiness:
    status: Literal["ok", "degraded", "unavailable"]
    checks: dict[str, bool]

    @property
    def http_status(self) -> int:
        return 503 if self.status == "unavailable" else 200

    def to_body(self) -> dict[str, object]:
        return {"status": self.status, "checks": self.checks}


Probe = Callable[[str, int], Awaitable[bool]]


async def tcp_reachable(host: str, port: int, timeout_s: float = 1.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_s)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


def dependency_from_url(name: str, url: str, *, required: bool = True) -> Dependency:
    parts = urlsplit(url)
    if not parts.hostname or parts.port is None:
        raise ValueError(f"{name}: URL must include a host and port")
    return Dependency(name, parts.hostname, parts.port, required)


async def evaluate_readiness(
    dependencies: Sequence[Dependency], probe: Probe = tcp_reachable
) -> Readiness:
    results = await asyncio.gather(*(probe(d.host, d.port) for d in dependencies))
    checks = {d.name: up for d, up in zip(dependencies, results, strict=True)}
    if any(not up for d, up in zip(dependencies, results, strict=True) if d.required):
        return Readiness("unavailable", checks)
    if not all(results):
        return Readiness("degraded", checks)
    return Readiness("ok", checks)
