"""Telemetry event schema (SPEC §6.1).

Changing a field, its order or its type requires bumping ``SCHEMA_VERSION`` and regenerating
``tests/contract/telemetry.snapshot.json`` with ``make telemetry-snapshot`` (IMPLEMENTATION B.5.4).
Events carry ids, the templated route, numbers and codes only: never query strings, header
values, bodies or secrets (SPEC §11.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

SCHEMA_VERSION: Final = 1
STREAM: Final = "telemetry"

Source = Literal["upstream", "jarvis"]
_SOURCES: Final = ("upstream", "jarvis")

# (name, type) in wire order; the snapshot pins this list.
EVENT_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("kind", "str"),
    ("v", "int"),
    ("request_id", "str"),
    ("ts", "str"),
    ("project_id", "str|null"),
    ("key_id", "str|null"),
    ("api_id", "str|null"),
    ("method", "str"),
    ("route", "str|null"),
    ("status", "int"),
    ("source", "str"),
    ("cache", "str|null"),
    ("cache_key_hash", "str|null"),
    ("attempts", "int"),
    ("latency_ms", "int"),
    ("upstream_latency_ms", "int|null"),
    ("req_bytes", "int"),
    ("resp_bytes", "int"),
    ("circuit", "str|null"),
    ("error_code", "str|null"),
    ("dedup_role", "str|null"),
)


@dataclass(frozen=True, slots=True)
class RequestEvent:
    request_id: str
    ts: str
    project_id: str | None
    key_id: str | None
    api_id: str | None
    method: str
    route: str | None
    status: int
    source: Source
    cache: str | None
    cache_key_hash: str | None
    attempts: int
    latency_ms: int
    upstream_latency_ms: int | None
    req_bytes: int
    resp_bytes: int
    circuit: str | None
    error_code: str | None
    dedup_role: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "request",
            "v": SCHEMA_VERSION,
            "request_id": self.request_id,
            "ts": self.ts,
            "project_id": self.project_id,
            "key_id": self.key_id,
            "api_id": self.api_id,
            "method": self.method,
            "route": self.route,
            "status": self.status,
            "source": self.source,
            "cache": self.cache,
            "cache_key_hash": self.cache_key_hash,
            "attempts": self.attempts,
            "latency_ms": self.latency_ms,
            "upstream_latency_ms": self.upstream_latency_ms,
            "req_bytes": self.req_bytes,
            "resp_bytes": self.resp_bytes,
            "circuit": self.circuit,
            "error_code": self.error_code,
            "dedup_role": self.dedup_role,
        }


def _count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def event_from_outcome(
    *,
    request_id: str,
    ts: str,
    project_id: str | None,
    key_id: str | None,
    api_id: str | None,
    method: str,
    route: str | None,
    status: int,
    source: str,
    attempts: int,
    latency_ms: int,
    upstream_latency_ms: int | None,
    req_bytes: int,
    resp_bytes: int,
    error_code: str | None,
    cache: str | None = None,
    cache_key_hash: str | None = None,
    circuit: str | None = None,
    dedup_role: str | None = None,
) -> RequestEvent:
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise ValueError("status must be an HTTP status code")
    if source not in _SOURCES:
        raise ValueError("source must be 'upstream' or 'jarvis'")
    if not method:
        raise ValueError("method must not be empty")
    if error_code is not None and not error_code.startswith("JV_"):
        raise ValueError("error_code must be a JV_* code")
    return RequestEvent(
        request_id=request_id,
        ts=ts,
        project_id=project_id,
        key_id=key_id,
        api_id=api_id,
        method=method,
        route=route,
        status=status,
        source=source,  # type: ignore[arg-type]  # narrowed by the check above
        cache=cache,
        cache_key_hash=cache_key_hash,
        attempts=_count("attempts", attempts),
        latency_ms=_count("latency_ms", latency_ms),
        upstream_latency_ms=(
            None
            if upstream_latency_ms is None
            else _count("upstream_latency_ms", upstream_latency_ms)
        ),
        req_bytes=_count("req_bytes", req_bytes),
        resp_bytes=_count("resp_bytes", resp_bytes),
        circuit=circuit,
        error_code=error_code,
        dedup_role=dedup_role,
    )


def encode(event: RequestEvent) -> str:
    return json.dumps(event.to_dict(), separators=(",", ":"), ensure_ascii=True)


def format_ts(epoch_seconds: float) -> str:
    """UTC, millisecond precision (truncated), ``Z`` suffix."""
    moment = datetime.fromtimestamp(epoch_seconds, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def snapshot_document() -> dict[str, Any]:
    return {"v": SCHEMA_VERSION, "stream": STREAM, "fields": [[n, t] for n, t in EVENT_FIELDS]}
