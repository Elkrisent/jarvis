"""Normalized internal request and auth context (SPEC §4.3 steps 1-2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

CACHEABLE_METHODS = frozenset({"GET", "HEAD"})


class AuthContext(BaseModel):
    """Result of authenticating a JARVIS key."""

    model_config = ConfigDict(frozen=True)

    project_id: str
    key_id: str
    allowed_api_ids: frozenset[str] | None = None  # None = every API in the project

    def allows(self, api_id: str) -> bool:
        return self.allowed_api_ids is None or api_id in self.allowed_api_ids


class NormalizedRequest(BaseModel):
    """One inbound request, after ingress, with credentials not yet resolved."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    api_slug: str
    method: str
    path: str = Field(description="Upstream path, always starting with '/'")
    query: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = Field(default=(), description="Lower-cased names")
    body: bytes = b""
    received_at: float = Field(description="Epoch seconds when the deadline clock started")

    @property
    def is_cacheable_method(self) -> bool:
        return self.method.upper() in CACHEABLE_METHODS

    @property
    def sorted_query(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self.query))

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key == wanted:
                return value
        return None
