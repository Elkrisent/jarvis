"""Uniform error responses: ``{"error": {"code", "message"}}``."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from jarvis_core.errors import (
    AuthRequiredError,
    ConflictError,
    JarvisError,
    NotFoundError,
    NotImplementedYetError,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

from jarvis_api.db.repositories import Conflict, NotFound

_BY_STATUS: dict[int, type[JarvisError]] = {
    404: NotFoundError,
    405: NotImplementedYetError,
}


def error_response(error: JarvisError) -> JSONResponse:
    body = {"error": {"code": error.code, "message": error.message}}
    headers = {"WWW-Authenticate": "Bearer"} if error.http_status == 401 else None
    return JSONResponse(body, status_code=error.http_status, headers=headers)


def sanitize_validation_errors(errors: list[Any]) -> list[dict[str, Any]]:
    """Keep type, location and message; drop ``input`` and ``ctx`` (they can hold secrets)."""
    return [
        {"type": e.get("type"), "loc": list(e.get("loc", ())), "msg": e.get("msg")} for e in errors
    ]


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(JarvisError)
    async def _jarvis(request: Request, exc: JarvisError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound) -> JSONResponse:
        return error_response(NotFoundError(str(exc)))

    @app.exception_handler(Conflict)
    async def _conflict(request: Request, exc: Conflict) -> JSONResponse:
        return error_response(ConflictError(str(exc)))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"detail": sanitize_validation_errors(list(exc.errors()))}, status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        cls = _BY_STATUS.get(exc.status_code)
        if cls is None:
            return JSONResponse(
                {"error": {"code": "JV_INTERNAL", "message": "error"}}, exc.status_code
            )
        response = error_response(cls(exc.detail if isinstance(exc.detail, str) else None))
        response.status_code = exc.status_code
        return response


__all__ = ["AuthRequiredError", "error_response", "install_error_handlers"]
