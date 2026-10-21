"""A controllable mock upstream API for tests and benchmarks.

Every request outside ``/__admin/`` is answered according to the first matching rule set
through the admin API, and recorded. With no matching rule it returns ``200`` and echoes the
request. See ``tools/mock_upstream/README.md``.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

Sleep = Callable[[float], Awaitable[None]]
CHUNK = 64 * 1024
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class ErrorRate(BaseModel):
    """Answer with ``status`` for a fraction ``rate`` of matching requests."""

    model_config = ConfigDict(extra="forbid")
    status: Annotated[int, Field(ge=400, le=599)]
    rate: Annotated[float, Field(ge=0.0, le=1.0)]


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # matching
    path_prefix: str = "/"
    methods: list[str] | None = None  # None = any method

    # timing
    latency_ms: Annotated[int, Field(ge=0)] = 0
    latency_range_ms: tuple[int, int] | None = None  # random in [min, max]; wins over latency_ms
    hang: bool = False  # never answer
    reset: bool = False  # start a response, then drop the connection mid-body

    # response
    status: Annotated[int, Field(ge=100, le=599)] = 200
    errors: list[ErrorRate] = Field(default_factory=list)
    retry_after_s: Annotated[int, Field(ge=0)] | None = None
    set_cookie: str | None = None
    cache_control: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body_bytes: Annotated[int, Field(ge=0, le=1_073_741_824)] | None = None  # streamed filler
    redirect_to: str | None = None  # Location; status becomes 302 unless 3xx is given

    @model_validator(mode="after")
    def _check(self) -> Rule:
        if self.latency_range_ms and self.latency_range_ms[0] > self.latency_range_ms[1]:
            raise ValueError("latency_range_ms must be [min, max] with min <= max")
        if self.latency_range_ms and self.latency_range_ms[0] < 0:
            raise ValueError("latency_range_ms must not be negative")
        if sum(e.rate for e in self.errors) > 1.0:
            raise ValueError("error rates must sum to at most 1.0")
        self.methods = [m.upper() for m in self.methods] if self.methods is not None else None
        return self

    def matches(self, method: str, path: str) -> bool:
        return path.startswith(self.path_prefix) and (
            self.methods is None or method.upper() in self.methods
        )


class MockState:
    def __init__(self, seed: int | None) -> None:
        self.initial_seed = seed
        self.rules: list[Rule] = []
        self.calls: list[dict[str, Any]] = []
        self.rng = random.Random(seed)

    def reset(self, seed: int | None = None) -> None:
        if seed is not None:
            self.initial_seed = seed
        self.rules = []
        self.calls = []
        self.rng = random.Random(self.initial_seed)


def _headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in request.headers.items():
        out[key] = f"{out[key]}, {value}" if key in out else value
    return out


async def _filler(total: int, *, abort_after: int | None = None) -> AsyncIterator[bytes]:
    sent = 0
    while sent < total:
        if abort_after is not None and sent >= abort_after:
            raise ConnectionResetError("mock upstream: injected connection drop")
        size = min(CHUNK, total - sent)
        if abort_after is not None:
            size = min(size, abort_after - sent)
        yield b"x" * size
        sent += size


def create_app(seed: int | None = None, sleep: Sleep = asyncio.sleep) -> FastAPI:
    app = FastAPI(title="jarvis-mock-upstream", docs_url=None, redoc_url=None, openapi_url=None)
    state = MockState(seed)
    app.state.mock = state

    # ------------------------------------------------------------------ admin API

    @app.get("/__admin/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/__admin/rules")
    async def set_rules(rules: Rule | list[Rule]) -> dict[str, int]:
        """Replace the rule set. Rules are tried in order; the first match wins."""
        state.rules = rules if isinstance(rules, list) else [rules]
        return {"rules": len(state.rules)}

    @app.get("/__admin/rules")
    async def get_rules() -> list[Rule]:
        return state.rules

    @app.get("/__admin/calls")
    async def get_calls(path_prefix: str = "") -> dict[str, Any]:
        calls = [c for c in state.calls if c["path"].startswith(path_prefix)]
        return {"count": len(calls), "calls": calls}

    @app.post("/__admin/reset")
    async def reset(seed: Annotated[int | None, Query()] = None) -> dict[str, str]:
        """Clear rules and calls, and restart the random sequence from the seed."""
        state.reset(seed)
        return {"status": "reset"}

    # ----------------------------------------------------------------- mock route

    @app.api_route("/{path:path}", methods=METHODS)
    async def mock(request: Request) -> Response:
        path = request.url.path
        body = await request.body()
        rule_index = next(
            (i for i, r in enumerate(state.rules) if r.matches(request.method, path)), None
        )
        headers = _headers(request)
        query = [[k, v] for k, v in request.query_params.multi_items()]
        state.calls.append(
            {
                "index": len(state.calls),
                "method": request.method,
                "path": path,
                "query": query,
                "headers": headers,
                "body_size": len(body),
                "rule": rule_index,
                "received_at": time.time(),
            }
        )
        rule = state.rules[rule_index] if rule_index is not None else Rule()

        if rule.latency_range_ms:
            await sleep(state.rng.uniform(*rule.latency_range_ms) / 1000)
        elif rule.latency_ms:
            await sleep(rule.latency_ms / 1000)

        if rule.hang:
            await asyncio.Event().wait()  # until the client gives up

        status = rule.status
        if rule.errors:
            draw = state.rng.random()
            edge = 0.0
            for err in rule.errors:
                edge += err.rate
                if draw < edge:
                    status = err.status
                    break

        extra = dict(rule.headers)
        if rule.retry_after_s is not None:
            extra["Retry-After"] = str(rule.retry_after_s)
        if rule.cache_control is not None:
            extra["Cache-Control"] = rule.cache_control
        if rule.set_cookie is not None:
            extra["Set-Cookie"] = rule.set_cookie

        if rule.reset:
            # Announce a body, send part of it, then fail: the server closes the connection.
            extra["Content-Length"] = str(max(rule.body_bytes or 1000, 2))
            return StreamingResponse(
                _filler(int(extra["Content-Length"]), abort_after=1),
                status_code=status,
                headers=extra,
            )

        if rule.redirect_to is not None:
            extra["Location"] = rule.redirect_to
            return Response(status_code=status if 300 <= status < 400 else 302, headers=extra)

        if rule.body_bytes is not None:
            extra["Content-Length"] = str(rule.body_bytes)
            return StreamingResponse(
                _filler(rule.body_bytes),
                status_code=status,
                headers=extra,
                media_type="application/octet-stream",
            )

        if status != rule.status:
            payload: dict[str, Any] = {"error": "injected", "status": status}
        else:
            payload = {
                "method": request.method,
                "path": path,
                "query": query,
                "headers": headers,
                "body_size": len(body),
            }
        return JSONResponse(payload, status_code=status, headers=extra)

    return app


def create_app_from_env() -> FastAPI:
    seed = os.environ.get("MOCK_SEED")
    return create_app(seed=int(seed) if seed else None)
