"""The ``/gw`` entry point (SPEC §4.1, §4.3): ingress, authentication, forwarding."""

from __future__ import annotations

import time
from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from jarvis_core.errors import (
    BodyTooLargeError,
    JarvisError,
    NotImplementedYetError,
    StreamingUnsupportedError,
)

from jarvis_gateway.auth import authenticate
from jarvis_gateway.config.store import ConfigStore
from jarvis_gateway.forward import wants_stream
from jarvis_gateway.forwarder import Forwarder
from jarvis_gateway.request_id import new_request_id

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _jarvis_response(status: int, body: Mapping[str, object], request_id: str) -> JSONResponse:
    return JSONResponse(
        body,
        status_code=status,
        headers={"X-Jarvis-Request-Id": request_id, "X-Jarvis-Source": "jarvis"},
    )


def _error(error: JarvisError, request_id: str) -> JSONResponse:
    error.request_id = request_id
    return _jarvis_response(error.http_status, error.to_body(), request_id)


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


def build_gw_router(store: ConfigStore, forwarder: Forwarder | None = None) -> APIRouter:
    """Without a forwarder an authenticated request still answers 501 (authentication only)."""
    router = APIRouter()

    async def gateway(api_slug: str, request: Request, path: str = "") -> Response:
        request_id = new_request_id()
        started = time.monotonic()  # the deadline clock starts at ingress (SPEC 4.3)
        try:
            if forwarder is not None:
                _ingress(request, forwarder)
            snapshot = store.current
            if snapshot is None:
                return _not_ready(request_id)
            headers = [
                (name.decode("latin-1"), value.decode("latin-1"))
                for name, value in request.headers.raw
            ]
            authenticated = authenticate(snapshot, api_slug, headers)
            if forwarder is None:
                raise NotImplementedYetError("forwarding is not implemented yet")
            return await forwarder.forward(request, authenticated, request_id, started)
        except JarvisError as error:
            return _error(error, request_id)

    router.add_api_route("/gw/{api_slug}", gateway, methods=METHODS, include_in_schema=False)
    router.add_api_route(
        "/gw/{api_slug}/{path:path}", gateway, methods=METHODS, include_in_schema=False
    )
    return router
