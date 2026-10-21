"""ApiDependency configuration model (SPEC §5.1) with validation (SPEC §4.5-4.11)."""

from __future__ import annotations

from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Slug = Annotated[str, Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")]
HeaderName = Annotated[str, Field(pattern=r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$", max_length=100)]
PositiveInt = Annotated[int, Field(gt=0)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credential(_Strict):
    """Credential injection (SPEC §4.5, D10). Exactly the fields the mode needs."""

    mode: Literal["bearer", "header", "query", "none"] = "none"
    header_name: HeaderName | None = None
    param_name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    secret_ref: Annotated[str, Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def _check_mode_fields(self) -> Self:
        if self.mode == "none":
            if self.secret_ref is not None:
                raise ValueError("secret_ref is not allowed when mode is 'none'")
        elif self.secret_ref is None:
            raise ValueError(f"secret_ref is required when mode is '{self.mode}'")
        if self.mode == "header" and self.header_name is None:
            raise ValueError("header_name is required when mode is 'header'")
        if self.mode == "query" and self.param_name is None:
            raise ValueError("param_name is required when mode is 'query'")
        if self.mode != "header" and self.header_name is not None:
            raise ValueError("header_name is only allowed when mode is 'header'")
        if self.mode != "query" and self.param_name is not None:
            raise ValueError("param_name is only allowed when mode is 'query'")
        return self


class RetryPolicy(_Strict):
    """SPEC §4.7."""

    enabled: bool = True
    max_attempts: Annotated[int, Field(ge=1, le=10)] = 3
    base_ms: PositiveInt = 100
    cap_ms: PositiveInt = 2000
    budget_ratio: Annotated[float, Field(gt=0.0, le=1.0)] = 0.2
    idempotency_header: HeaderName | None = None

    @model_validator(mode="after")
    def _cap_at_least_base(self) -> Self:
        if self.cap_ms < self.base_ms:
            raise ValueError("cap_ms must be >= base_ms")
        return self


class CircuitPolicy(_Strict):
    """SPEC §4.8."""

    failure_rate_threshold: Annotated[float, Field(gt=0.0, le=1.0)] = 0.5
    window_s: PositiveInt = 30
    min_calls: PositiveInt = 20
    open_duration_s: PositiveInt = 30
    max_open_duration_s: PositiveInt = 300

    @model_validator(mode="after")
    def _max_open_at_least_open(self) -> Self:
        if self.max_open_duration_s < self.open_duration_s:
            raise ValueError("max_open_duration_s must be >= open_duration_s")
        return self


class CachePolicy(_Strict):
    """SPEC §4.9."""

    enabled: bool = False
    default_ttl_s: Annotated[int, Field(ge=0)] = 60
    stale_ttl_s: Annotated[int, Field(ge=0)] = 600
    honor_cache_control: bool = False
    vary_headers: tuple[HeaderName, ...] = ("accept", "accept-language")
    max_object_bytes: PositiveInt = 1_048_576

    @field_validator("vary_headers")
    @classmethod
    def _lower_vary(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({v.lower() for v in value}))


class BucketPolicy(_Strict):
    """Token bucket parameters (SPEC §4.11)."""

    enabled: bool = False
    capacity: PositiveInt = 100
    refill_per_s: Annotated[float, Field(gt=0.0)] = 10.0


class RateLimits(_Strict):
    inbound: BucketPolicy = BucketPolicy()
    upstream: BucketPolicy = BucketPolicy()


class Fallback(_Strict):
    """SPEC §4.12. No chains: the fallback API's own fallback is never followed."""

    serve_stale: bool = False
    fallback_api: Slug | None = None


class Route(_Strict):
    pattern: Annotated[str, Field(min_length=1, max_length=500)]
    cache_ttl_s: Annotated[int, Field(ge=0)] | None = None

    @field_validator("pattern")
    @classmethod
    def _starts_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("route pattern must start with '/'")
        return value


class BodySampling(_Strict):
    enabled: bool = False
    rate: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0


class ApiDependency(_Strict):
    """Canonical per-API configuration."""

    slug: Slug
    name: Annotated[str, Field(min_length=1, max_length=200)]
    base_url: str
    credential: Credential = Credential()
    jarvis_key_slot: HeaderName = "x-jarvis-key"
    deadline_ms: PositiveInt = 5000
    attempt_timeout_ms: PositiveInt = 2000
    min_attempt_ms: PositiveInt = 300
    retry: RetryPolicy = RetryPolicy()
    circuit: CircuitPolicy = CircuitPolicy()
    cache: CachePolicy = CachePolicy()
    rate_limits: RateLimits = RateLimits()
    fallback: Fallback = Fallback()
    routes: tuple[Route, ...] = ()
    body_sampling: BodySampling = BodySampling()

    @field_validator("base_url")
    @classmethod
    def _valid_base_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("base_url must be an absolute http(s) URL with a host")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("base_url must not contain credentials, a query or a fragment")
        return value.rstrip("/")

    @field_validator("jarvis_key_slot")
    @classmethod
    def _lower_slot(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Self:
        if self.attempt_timeout_ms > self.deadline_ms:
            raise ValueError("attempt_timeout_ms must be <= deadline_ms")
        if self.min_attempt_ms > self.deadline_ms:
            raise ValueError("min_attempt_ms must be <= deadline_ms")
        if self.fallback.fallback_api == self.slug:
            raise ValueError("fallback_api must not point at the API itself")
        patterns = [r.pattern for r in self.routes]
        if len(patterns) != len(set(patterns)):
            raise ValueError("route patterns must be unique")
        return self
