"""Startup settings, loaded and validated from the environment."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from jarvis_core.secretbox import AesGcmSecretBox, MasterKeyError, secret_box_from_env
from jarvis_core.ssrf import SsrfPolicy

DEFAULT_SNAPSHOT_PATH = "/var/lib/jarvis/gateway/snapshot.json"
DEFAULT_RESYNC_INTERVAL_S = 60.0
MIN_RESYNC_INTERVAL_S = 1.0
DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_AGE_S = 86_400.0
MIN_SNAPSHOT_MAX_AGE_S = 1.0
DEFAULT_TELEMETRY_QUEUE_SIZE = 10_000
DEFAULT_TELEMETRY_BATCH_SIZE = 500
DEFAULT_TELEMETRY_MAXLEN = 1_000_000
_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


class SettingsError(Exception):
    """Invalid or missing configuration. Messages name variables, never values."""


def redact_url(url: str) -> str:
    """The URL with any password replaced by ``***``; an unparseable URL is not shown at all."""
    try:
        parts = urlsplit(url)
        if parts.password is None:
            return url
        host = parts.netloc.rpartition("@")[2]
        user = f"{parts.username}:" if parts.username else ":"
        return parts._replace(netloc=f"{user}***@{host}").geturl()
    except ValueError:
        return "<unparseable url>"


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    snapshot_path: Path
    resync_interval_s: float
    secret_box: AesGcmSecretBox = field(repr=False)
    snapshot_max_age_s: float = DEFAULT_SNAPSHOT_MAX_AGE_S
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    ssrf_policy: SsrfPolicy = field(default_factory=SsrfPolicy, repr=False)
    telemetry_enabled: bool = True
    telemetry_queue_size: int = DEFAULT_TELEMETRY_QUEUE_SIZE
    telemetry_batch_size: int = DEFAULT_TELEMETRY_BATCH_SIZE
    telemetry_maxlen: int = DEFAULT_TELEMETRY_MAXLEN
    expected_instances: int = 1
    redis_fail_open: bool = True

    def __repr__(self) -> str:
        return (
            f"Settings(database_url={redact_url(self.database_url)!r}, "
            f"redis_url={redact_url(self.redis_url)!r}, snapshot_path={str(self.snapshot_path)!r}, "
            f"resync_interval_s={self.resync_interval_s!r}, "
            f"snapshot_max_age_s={self.snapshot_max_age_s!r}, "
            f"max_body_bytes={self.max_body_bytes!r}, "
            f"telemetry_enabled={self.telemetry_enabled!r})"
        )

    __str__ = __repr__


def _require(source: Mapping[str, str], name: str) -> str:
    value = source.get(name)
    if not value:
        raise SettingsError(f"{name} is not set")
    return value


def _interval(source: Mapping[str, str]) -> float:
    raw = source.get("JARVIS_RESYNC_INTERVAL_S")
    if raw is None or raw == "":
        return DEFAULT_RESYNC_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError("JARVIS_RESYNC_INTERVAL_S must be a number") from None
    if not math.isfinite(value) or value < MIN_RESYNC_INTERVAL_S:
        raise SettingsError(
            f"JARVIS_RESYNC_INTERVAL_S must be a finite number of at least "
            f"{MIN_RESYNC_INTERVAL_S:g} seconds"
        )
    return value


def _snapshot_max_age(source: Mapping[str, str]) -> float:
    raw = source.get("JARVIS_SNAPSHOT_MAX_AGE_S")
    if raw is None or raw == "":
        return DEFAULT_SNAPSHOT_MAX_AGE_S
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError("JARVIS_SNAPSHOT_MAX_AGE_S must be a number") from None
    if not math.isfinite(value) or value < MIN_SNAPSHOT_MAX_AGE_S:
        raise SettingsError(
            f"JARVIS_SNAPSHOT_MAX_AGE_S must be a finite number of at least "
            f"{MIN_SNAPSHOT_MAX_AGE_S:g} seconds"
        )
    return value


def _max_body_bytes(source: Mapping[str, str]) -> int:
    raw = source.get("JARVIS_MAX_BODY_BYTES")
    if raw is None or raw == "":
        return DEFAULT_MAX_BODY_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError("JARVIS_MAX_BODY_BYTES must be a whole number of bytes") from None
    if value < 1:
        raise SettingsError("JARVIS_MAX_BODY_BYTES must be at least 1")
    return value


def _positive_int(source: Mapping[str, str], name: str, default: int) -> int:
    raw = source.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a whole number") from None
    if value < 1:
        raise SettingsError(f"{name} must be at least 1")
    return value


def _bool(source: Mapping[str, str], name: str, default: str) -> bool:
    raw = (source.get(name) or default).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise SettingsError(f"{name} must be true or false")


def _telemetry_enabled(source: Mapping[str, str]) -> bool:
    return _bool(source, "JARVIS_TELEMETRY_ENABLED", "true")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    try:
        secret_box = secret_box_from_env(source)
    except MasterKeyError:
        raise SettingsError("JARVIS_MASTER_KEY is missing or invalid") from None
    try:
        ssrf_policy = SsrfPolicy.from_env(source)
    except ValueError as exc:  # names the variable and the entry number only
        raise SettingsError(str(exc)) from None
    return Settings(
        database_url=_require(source, "DATABASE_URL"),
        redis_url=_require(source, "REDIS_URL"),
        snapshot_path=Path(source.get("JARVIS_SNAPSHOT_PATH") or DEFAULT_SNAPSHOT_PATH),
        resync_interval_s=_interval(source),
        snapshot_max_age_s=_snapshot_max_age(source),
        secret_box=secret_box,
        max_body_bytes=_max_body_bytes(source),
        ssrf_policy=ssrf_policy,
        telemetry_enabled=_telemetry_enabled(source),
        telemetry_queue_size=_positive_int(
            source, "JARVIS_TELEMETRY_QUEUE_SIZE", DEFAULT_TELEMETRY_QUEUE_SIZE
        ),
        telemetry_batch_size=_positive_int(
            source, "JARVIS_TELEMETRY_BATCH_SIZE", DEFAULT_TELEMETRY_BATCH_SIZE
        ),
        telemetry_maxlen=_positive_int(
            source, "JARVIS_TELEMETRY_STREAM_MAXLEN", DEFAULT_TELEMETRY_MAXLEN
        ),
        expected_instances=_positive_int(source, "JARVIS_EXPECTED_INSTANCES", 1),
        redis_fail_open=_bool(source, "JARVIS_REDIS_FAIL_OPEN", "true"),
    )
