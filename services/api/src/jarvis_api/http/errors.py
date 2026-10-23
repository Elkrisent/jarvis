"""Uniform error responses: ``{"error": {"code", "message"}}``."""

from __future__ import annotations

from http import HTTPStatus
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
from jarvis_api.http.deps import API_PREFIX, require_admin

_BY_STATUS: dict[int, type[JarvisError]] = {
    401: AuthRequiredError,
    404: NotFoundError,
    405: NotImplementedYetError,
    409: ConflictError,
    501: NotImplementedYetError,
}

INTERNAL_MESSAGE = "internal error"


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
        guarded = getattr(request.app.state, "admin_token", None) is not None
        if exc.status_code in (404, 405) and guarded and _under_api(request):
            # No route ran, so no token check either: never reveal which paths exist.
            try:
                require_admin(request, request.headers.get("authorization"))
            except AuthRequiredError as denied:
                return error_response(denied)
        detail = exc.detail
        message = detail if isinstance(detail, str) and detail else _phrase(exc.status_code)
        cls = _BY_STATUS.get(exc.status_code, JarvisError)
        body = {"error": {"code": cls.code, "message": message}}
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Never echo the exception: its text can hold connection strings or request data.
        return error_response(JarvisError(INTERNAL_MESSAGE))


def _under_api(request: Request) -> bool:
    path: str = request.scope["path"]
    return path == API_PREFIX or path.startswith(API_PREFIX + "/")


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "error"


__all__ = ["AuthRequiredError", "error_response", "install_error_handlers"]
