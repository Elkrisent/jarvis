from pathlib import Path

import pytest
from jarvis_gateway.settings import SettingsError, load_settings

pytestmark = pytest.mark.unit

BASE = {
    "DATABASE_URL": "postgresql://u:s3cret@postgres:5432/j",
    "REDIS_URL": "redis://redis:6379/0",
}


def test_defaults() -> None:
    s = load_settings(BASE)
    assert s.resync_interval_s == 60.0
    assert s.snapshot_path == Path("/var/lib/jarvis/gateway/snapshot.json")
    assert s.redis_url == BASE["REDIS_URL"]


def test_overrides() -> None:
    s = load_settings(
        {**BASE, "JARVIS_RESYNC_INTERVAL_S": "2.5", "JARVIS_SNAPSHOT_PATH": "/tmp/x.json"}
    )
    assert (s.resync_interval_s, s.snapshot_path) == (2.5, Path("/tmp/x.json"))


@pytest.mark.parametrize("name", ["DATABASE_URL", "REDIS_URL"])
def test_missing_required_variable_is_named(name: str) -> None:
    env = {k: v for k, v in BASE.items() if k != name}
    with pytest.raises(SettingsError, match=name):
        load_settings(env)


@pytest.mark.parametrize("bad", ["0", "0.5", "-3", "abc", "nan", "inf"])
def test_invalid_interval_is_rejected_by_name_without_the_value(bad: str) -> None:
    with pytest.raises(SettingsError, match="JARVIS_RESYNC_INTERVAL_S") as exc:
        load_settings({**BASE, "JARVIS_RESYNC_INTERVAL_S": bad})
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
