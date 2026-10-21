"""JARVIS-generated errors (SPEC §4.4).

Errors returned by an upstream are passed through unchanged and are not modelled here.
"""

from __future__ import annotations

from typing import Any, ClassVar


class JarvisError(Exception):
    """Base class for every error JARVIS itself generates."""

    code: ClassVar[str] = "JV_INTERNAL"
    http_status: ClassVar[int] = 500

    def __init__(self, message: str | None = None, *, request_id: str | None = None) -> None:
        self.message = message or self.code
        self.request_id = request_id
        super().__init__(self.message)

    def to_body(self) -> dict[str, Any]:
        """The JSON body from SPEC §4.4."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "request_id": self.request_id,
            }
        }


class AuthInvalidError(JarvisError):
    code = "JV_AUTH_INVALID"
    http_status = 401


class RateLimitedError(JarvisError):
    code = "JV_RATE_LIMITED"
    http_status = 429


class QuotaExceededError(JarvisError):
    code = "JV_QUOTA_EXCEEDED"
    http_status = 429


class CircuitOpenError(JarvisError):
    code = "JV_CIRCUIT_OPEN"
    http_status = 503


class UpstreamTimeoutError(JarvisError):
    code = "JV_UPSTREAM_TIMEOUT"
    http_status = 504


class UpstreamUnreachableError(JarvisError):
    code = "JV_UPSTREAM_UNREACHABLE"
    http_status = 502


class BodyTooLargeError(JarvisError):
    code = "JV_BODY_TOO_LARGE"
    http_status = 413


class StreamingUnsupportedError(JarvisError):
    code = "JV_STREAMING_UNSUPPORTED"
    http_status = 400


class DestinationBlockedError(JarvisError):
    code = "JV_DESTINATION_BLOCKED"
    http_status = 403


ALL_ERRORS: tuple[type[JarvisError], ...] = (
    AuthInvalidError,
    RateLimitedError,
    QuotaExceededError,
    CircuitOpenError,
    UpstreamTimeoutError,
    UpstreamUnreachableError,
    BodyTooLargeError,
    StreamingUnsupportedError,
    DestinationBlockedError,
)

_BY_CODE: dict[str, type[JarvisError]] = {cls.code: cls for cls in ALL_ERRORS}


def error_for_code(code: str) -> type[JarvisError]:
    """Look up the error class for a ``JV_*`` code."""
    try:
        return _BY_CODE[code]
    except KeyError:
        raise KeyError(f"unknown JARVIS error code: {code}") from None
