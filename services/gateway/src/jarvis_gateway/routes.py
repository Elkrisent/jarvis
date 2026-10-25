"""The ``/gw`` entry point (SPEC §4.1, §4.3): ingress, authentication, forwarding."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from jarvis_core.clock import Clock, SystemClock
from jarvis_core.errors import (
    BodyTooLargeError,
    CircuitOpenError,
    JarvisError,
    NotImplementedYetError,
    QuotaExceededError,
    RateLimitedError,
    StreamingUnsupportedError,
)
from jarvis_core.interfaces import TelemetrySink
from jarvis_core.telemetry import format_ts

from jarvis_gateway.auth import authenticate
from jarvis_gateway.config.store import ConfigStore
from jarvis_gateway.forward import wants_stream
from jarvis_gateway.forwarder import Forwarder
from jarvis_gateway.record import RequestRecord
from jarvis_gateway.request_id import new_request_id
from jarvis_gateway.routing import TemplateRegistry
from jarvis_gateway.telemetry import NoopTelemetrySink

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _jarvis_response(
    status: int,
    body: Mapping[str, object],
    request_id: str,
    circuit: str | None = None,
    retry_after: str | None = None,
) -> JSONResponse:
    headers = {"X-Jarvis-Request-Id": request_id, "X-Jarvis-Source": "jarvis"}
    if circuit is not None:
        headers["X-Jarvis-Circuit"] = circuit
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return JSONResponse(body, status_code=status, headers=headers)


def _error(error: JarvisError, request_id: str) -> JSONResponse:
    error.request_id = request_id
    circuit = error.circuit_state if isinstance(error, CircuitOpenError) else None
    retry_after = None
    if isinstance(error, RateLimitedError | QuotaExceededError) and error.retry_after_s is not None:
        retry_after = str(max(1, math.ceil(error.retry_after_s)))
    return _jarvis_response(error.http_status, error.to_body(), request_id, circuit, retry_after)


def _ingress(request: Request, forwarder: Forwarder) -> None:
    """SPEC §4.3 step 1: reject what is refused before any key is looked at."""
    if wants_stream(request.headers.get("accept")):
        raise StreamingUnsupportedError("streaming responses are not supported")
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > forwarder.max_body_bytes:
        raise BodyTooLargeError("request body is too large")


def _not_ready(request_id: str) -> JSONResponse:
    body = {
        "error": {
            "code": "JV_NOT_READY",
            "message": "gateway configuration is not loaded yet",
            "request_id": request_id,
        }
    }
    return _jarvis_response(503, body, request_id)


def build_gw_router(
    store: ConfigStore,
    forwarder: Forwarder | None = None,
    sink: TelemetrySink | None = None,
    templates: TemplateRegistry | None = None,
    clock: Clock | None = None,
) -> APIRouter:
    """Without a forwarder an authenticated request still answers 501 (authentication only)."""
    router = APIRouter()
    sink = NoopTelemetrySink() if sink is None else sink
    templates = TemplateRegistry() if templates is None else templates
    clock = SystemClock() if clock is None else clock

    async def gateway(api_slug: str, request: Request, path: str = "") -> Response:
        request_id = new_request_id()
        started = time.monotonic()  # the deadline clock starts at ingress (SPEC 4.3)
        record = RequestRecord(
            sink,
            request_id=request_id,
            method=request.method,
            started=started,
            ts=format_ts(clock.now()),
            monotonic=time.monotonic,
        )
        try:
            if forwarder is not None:
                _ingress(request, forwarder)
            snapshot = store.current
            if snapshot is None:
                record.finish(status=503, source="jarvis", error_code="JV_NOT_READY")
                return _not_ready(request_id)
            headers = [
                (name.decode("latin-1"), value.decode("latin-1"))
                for name, value in request.headers.raw
            ]
            authenticated = authenticate(snapshot, api_slug, headers)
            record.identify(
                project_id=authenticated.auth.project_id,
                key_id=authenticated.auth.key_id,
                api_id=authenticated.api.api_id,
                route=templates.route_for(authenticated.api, "/" + path),
            )
            if forwarder is None:
                raise NotImplementedYetError("forwarding is not implemented yet")
            await forwarder.check_inbound(authenticated)  # pipeline step 3
            return await forwarder.forward(request, authenticated, request_id, started, record)
        except JarvisError as error:
            record.finish(status=error.http_status, source="jarvis", error_code=error.code)
            return _error(error, request_id)
        except asyncio.CancelledError:
            record.finish(status=499, source="jarvis", error_code=None)  # the caller went away
            raise
        except Exception:
            # An unexpected failure becomes a 500 from the framework; it still gets its event.
            record.finish(status=500, source="jarvis", error_code="JV_INTERNAL")
            raise

    router.add_api_route("/gw/{api_slug}", gateway, methods=METHODS, include_in_schema=False)
    router.add_api_route(
        "/gw/{api_slug}/{path:path}", gateway, methods=METHODS, include_in_schema=False
    )
    return router
