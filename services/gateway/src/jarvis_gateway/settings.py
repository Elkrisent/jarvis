"""Startup settings, loaded and validated from the environment."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_SNAPSHOT_PATH = "/var/lib/jarvis/gateway/snapshot.json"
DEFAULT_RESYNC_INTERVAL_S = 60.0
MIN_RESYNC_INTERVAL_S = 1.0


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

    def __repr__(self) -> str:
        return (
            f"Settings(database_url={redact_url(self.database_url)!r}, "
            f"redis_url={redact_url(self.redis_url)!r}, snapshot_path={str(self.snapshot_path)!r}, "
            f"resync_interval_s={self.resync_interval_s!r})"
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


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    return Settings(
        database_url=_require(source, "DATABASE_URL"),
        redis_url=_require(source, "REDIS_URL"),
        snapshot_path=Path(source.get("JARVIS_SNAPSHOT_PATH") or DEFAULT_SNAPSHOT_PATH),
        resync_interval_s=_interval(source),
    )
