"""The ``/gw`` entry point (SPEC §4.1). Authentication only until forwarding arrives in M1.8."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from jarvis_core.errors import JarvisError, NotImplementedYetError

from jarvis_gateway.auth import authenticate
from jarvis_gateway.config.store import ConfigStore
from jarvis_gateway.request_id import new_request_id

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _jarvis_response(status: int, body: dict[str, object], request_id: str) -> JSONResponse:
    return JSONResponse(
        body,
        status_code=status,
        headers={"X-Jarvis-Request-Id": request_id, "X-Jarvis-Source": "jarvis"},
    )


def _error(error: JarvisError, request_id: str) -> JSONResponse:
    error.request_id = request_id
    return _jarvis_response(error.http_status, error.to_body(), request_id)


def build_gw_router(store: ConfigStore) -> APIRouter:
    router = APIRouter()

    async def gateway(api_slug: str, request: Request, path: str = "") -> Response:
        request_id = new_request_id()
        snapshot = store.current
        if snapshot is None:
            body = {
                "error": {
                    "code": "JV_NOT_READY",
                    "message": "gateway configuration is not loaded yet",
                    "request_id": request_id,
                }
            }
            return _jarvis_response(503, body, request_id)
        headers = [
            (name.decode("latin-1"), value.decode("latin-1")) for name, value in request.headers.raw
        ]
        try:
            authenticate(snapshot, api_slug, headers)
        except JarvisError as error:
            return _error(error, request_id)
        # Authenticated. M1.8 replaces this with the forwarded response.
        return _error(NotImplementedYetError("forwarding is not implemented yet"), request_id)

    router.add_api_route("/gw/{api_slug}", gateway, methods=METHODS, include_in_schema=False)
    router.add_api_route(
        "/gw/{api_slug}/{path:path}", gateway, methods=METHODS, include_in_schema=False
    )
    return router
