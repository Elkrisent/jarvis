import base64
from pathlib import Path

import pytest
from jarvis_gateway.settings import SettingsError, load_settings

pytestmark = pytest.mark.unit

MASTER_KEY = base64.b64encode(bytes(range(32))).decode()
BASE = {
    "DATABASE_URL": "postgresql://u:s3cret@postgres:5432/j",
    "REDIS_URL": "redis://redis:6379/0",
    "JARVIS_MASTER_KEY": MASTER_KEY,
}


def test_defaults() -> None:
    s = load_settings(BASE)
    assert s.resync_interval_s == 60.0
    assert s.snapshot_path == Path("/var/lib/jarvis/gateway/snapshot.json")
    assert s.redis_url == BASE["REDIS_URL"]
    assert s.snapshot_max_age_s == 86_400.0


def test_overrides() -> None:
    s = load_settings(
        {**BASE, "JARVIS_RESYNC_INTERVAL_S": "2.5", "JARVIS_SNAPSHOT_PATH": "/tmp/x.json"}
    )
    assert (s.resync_interval_s, s.snapshot_path) == (2.5, Path("/tmp/x.json"))


@pytest.mark.parametrize("name", ["DATABASE_URL", "REDIS_URL", "JARVIS_MASTER_KEY"])
def test_missing_required_variable_is_named(name: str) -> None:
    env = {k: v for k, v in BASE.items() if k != name}
    with pytest.raises(SettingsError, match=name):
        load_settings(env)


@pytest.mark.parametrize("bad", ["0", "0.5", "-3", "abc", "nan", "inf"])
def test_invalid_interval_is_rejected_by_name_without_the_value(bad: str) -> None:
    with pytest.raises(SettingsError, match="JARVIS_RESYNC_INTERVAL_S") as exc:
        load_settings({**BASE, "JARVIS_RESYNC_INTERVAL_S": bad})
    assert f"'{bad}'" not in str(exc.value)


def test_snapshot_max_age_override() -> None:
    assert load_settings({**BASE, "JARVIS_SNAPSHOT_MAX_AGE_S": "600"}).snapshot_max_age_s == 600.0


@pytest.mark.parametrize("bad", ["0", "0.5", "-3", "abc", "nan", "inf"])
def test_invalid_snapshot_max_age_is_rejected_by_name_without_the_value(bad: str) -> None:
    with pytest.raises(SettingsError, match="JARVIS_SNAPSHOT_MAX_AGE_S") as exc:
        load_settings({**BASE, "JARVIS_SNAPSHOT_MAX_AGE_S": bad})
    assert f"'{bad}'" not in str(exc.value)


def test_repr_hides_passwords() -> None:
    s = load_settings({**BASE, "REDIS_URL": "redis://:rpw@redis:6379/0"})
    for text in (repr(s), str(s)):
        assert "s3cret" not in text
        assert "rpw" not in text
        assert "***" in text


def test_unparseable_url_is_not_shown() -> None:
    s = load_settings({**BASE, "DATABASE_URL": "postgresql://u:p@[bad"})
    assert "[bad" not in repr(s)


def test_master_key_builds_a_secret_box_and_is_never_shown() -> None:
    settings = load_settings(BASE)
    assert settings.secret_box.key_id.startswith("k_")
    for text in (repr(settings), str(settings)):
        assert MASTER_KEY not in text


@pytest.mark.parametrize("bad", ["not base64!", "c2hvcnQ=", ""])
def test_a_malformed_master_key_is_rejected_by_name_without_the_value(bad: str) -> None:
    with pytest.raises(SettingsError, match="JARVIS_MASTER_KEY") as exc:
        load_settings({**BASE, "JARVIS_MASTER_KEY": bad})
    assert bad not in str(exc.value) or bad == ""


def test_max_body_bytes_default_override_and_validation() -> None:
    assert load_settings(BASE).max_body_bytes == 10 * 1024 * 1024
    assert load_settings({**BASE, "JARVIS_MAX_BODY_BYTES": "2048"}).max_body_bytes == 2048
    for bad in ("0", "-1", "1.5", "abc", "nan"):
        with pytest.raises(SettingsError, match="JARVIS_MAX_BODY_BYTES") as exc:
            load_settings({**BASE, "JARVIS_MAX_BODY_BYTES": bad})
        assert f"'{bad}'" not in str(exc.value)


def test_the_ssrf_policy_comes_from_the_environment_and_errors_name_the_variable() -> None:
    policy = load_settings({**BASE, "JARVIS_SSRF_ALLOWLIST": "10.0.0.0/8 svc.internal"}).ssrf_policy
    assert policy.allow_hosts == frozenset({"svc.internal"})
    with pytest.raises(SettingsError, match="JARVIS_SSRF_ALLOWLIST"):
        load_settings({**BASE, "JARVIS_SSRF_ALLOWLIST": "10.0.0.0/99"})
    assert "10.0.0.0/8" not in repr(load_settings({**BASE, "JARVIS_SSRF_ALLOWLIST": "10.0.0.0/8"}))
