"""Startup settings loading and validation."""

import base64

import pytest
from jarvis_api.settings import SettingsError, load_settings

TOKEN = "t" * 32
MASTER = base64.b64encode(bytes(range(32))).decode()
ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "JARVIS_ADMIN_TOKEN": TOKEN,
    "JARVIS_MASTER_KEY": MASTER,
}


@pytest.mark.unit
def test_valid_env_loads() -> None:
    settings = load_settings(ENV)
    assert settings.database_url == ENV["DATABASE_URL"]
    assert settings.redis_url == ENV["REDIS_URL"]
    assert settings.admin_token == TOKEN
    assert settings.secret_box.open(settings.secret_box.seal("x")) == "x"


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(ENV))
def test_missing_variable_is_named(name: str) -> None:
    env = {k: v for k, v in ENV.items() if k != name}
    with pytest.raises(SettingsError, match=name):
        load_settings(env)


@pytest.mark.unit
def test_admin_token_minimum_length() -> None:
    with pytest.raises(SettingsError, match="JARVIS_ADMIN_TOKEN"):
        load_settings({**ENV, "JARVIS_ADMIN_TOKEN": "t" * 31})
    assert load_settings({**ENV, "JARVIS_ADMIN_TOKEN": "t" * 32}).admin_token == "t" * 32


@pytest.mark.unit
def test_bad_master_key_rejected_without_echo() -> None:
    bad = "not-a-valid-key-SECRETVALUE"
    with pytest.raises(SettingsError, match="JARVIS_MASTER_KEY") as info:
        load_settings({**ENV, "JARVIS_MASTER_KEY": bad})
    assert bad not in str(info.value)


@pytest.mark.unit
def test_repr_and_str_hide_secrets() -> None:
    settings = load_settings(ENV)
    for text in (repr(settings), str(settings)):
        assert TOKEN not in text
        assert MASTER not in text
