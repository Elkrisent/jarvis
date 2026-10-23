"""Startup settings, loaded and validated from the environment."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from jarvis_core.secretbox import AesGcmSecretBox, MasterKeyError, secret_box_from_env

MIN_ADMIN_TOKEN_LEN = 32


class SettingsError(Exception):
    """Invalid or missing configuration. Messages name variables, never values."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    admin_token: str
    secret_box: AesGcmSecretBox

    def __repr__(self) -> str:
        return f"Settings(database_url={self.database_url!r}, redis_url={self.redis_url!r})"

    __str__ = __repr__


def _require(source: Mapping[str, str], name: str) -> str:
    value = source.get(name)
    if not value:
        raise SettingsError(f"{name} is not set")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    database_url = _require(source, "DATABASE_URL")
    redis_url = _require(source, "REDIS_URL")
    admin_token = _require(source, "JARVIS_ADMIN_TOKEN")
    if len(admin_token) < MIN_ADMIN_TOKEN_LEN:
        raise SettingsError(f"JARVIS_ADMIN_TOKEN must be at least {MIN_ADMIN_TOKEN_LEN} characters")
    _require(source, "JARVIS_MASTER_KEY")
    try:
        box = secret_box_from_env(source)
    except MasterKeyError as exc:
        raise SettingsError(f"JARVIS_MASTER_KEY is invalid: {exc}") from None
    return Settings(database_url, redis_url, admin_token, box)
