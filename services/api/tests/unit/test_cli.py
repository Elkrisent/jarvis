"""`jarvis rotate-master-key` argument and configuration validation (no database needed)."""

from __future__ import annotations

import base64

import pytest
from jarvis_api.cli import main

pytestmark = [pytest.mark.unit, pytest.mark.req("REQ-11.2-3")]

OLD_B64 = base64.b64encode(bytes(range(32))).decode()
NEW_B64 = base64.b64encode(bytes(range(100, 132))).decode()
DB = "postgresql://u:pw@127.0.0.1:1/db"


def _env(**over: str | None) -> dict[str, str]:
    env = {
        "JARVIS_MASTER_KEY": OLD_B64,
        "JARVIS_NEW_MASTER_KEY": NEW_B64,
        "DATABASE_URL": DB,
    }
    for name, value in over.items():
        if value is None:
            env.pop(name)
        else:
            env[name] = value
    return env


def test_no_subcommand_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([], env=_env()) == 2
    assert capsys.readouterr().out == ""


def test_missing_old_key_names_the_variable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rotate-master-key"], env=_env(JARVIS_MASTER_KEY=None)) == 2
    err = capsys.readouterr().err
    assert "JARVIS_MASTER_KEY" in err
    assert len(err.splitlines()) == 1


def test_missing_new_key_names_the_variable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rotate-master-key"], env=_env(JARVIS_NEW_MASTER_KEY=None)) == 2
    assert "JARVIS_NEW_MASTER_KEY" in capsys.readouterr().err


def test_invalid_new_key_is_rejected_without_echoing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = "not-a-valid-key-SECRETMARKER"
    assert main(["rotate-master-key"], env=_env(JARVIS_NEW_MASTER_KEY=bad)) == 2
    captured = capsys.readouterr()
    assert "JARVIS_NEW_MASTER_KEY" in captured.err
    assert "invalid" in captured.err
    assert "SECRETMARKER" not in captured.out + captured.err
    assert len(captured.err.splitlines()) == 1


def test_invalid_old_key_is_rejected_without_echoing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = base64.b64encode(b"short").decode()
    assert main(["rotate-master-key"], env=_env(JARVIS_MASTER_KEY=bad)) == 2
    captured = capsys.readouterr()
    assert "JARVIS_MASTER_KEY" in captured.err
    assert bad not in captured.out + captured.err


def test_refuses_when_the_new_key_equals_the_old_key(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rotate-master-key"], env=_env(JARVIS_NEW_MASTER_KEY=OLD_B64)) == 2
    err = capsys.readouterr().err
    assert "identical" in err
    assert OLD_B64 not in err


def test_missing_database_url_names_the_variable(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rotate-master-key"], env=_env(DATABASE_URL=None)) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_unknown_argument_is_a_one_line_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rotate-master-key", "--key", OLD_B64], env=_env()) == 2
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert OLD_B64 not in err


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"], env=_env()) == 0
    assert "rotate-master-key" in capsys.readouterr().out


def _assert_clean_failure(
    capsys: pytest.CaptureFixture[str], password: str, expected: str | None = None
) -> None:
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    if expected is not None:
        assert captured.err.strip() == expected
    for secret in (password, OLD_B64, NEW_B64):
        assert secret not in captured.out + captured.err


def test_non_numeric_port_is_invalid_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    url = "postgresql://u:pwNONNUM@127.0.0.1:abc/db"
    assert main(["rotate-master-key"], env=_env(DATABASE_URL=url)) == 2
    _assert_clean_failure(capsys, "pwNONNUM", "DATABASE_URL is invalid")


def test_unparseable_url_is_invalid_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rotate-master-key"], env=_env(DATABASE_URL="not a url")) == 2
    _assert_clean_failure(capsys, "pwUNUSED", "DATABASE_URL is invalid")


def test_out_of_range_port_exits_1_with_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    url = "postgresql://u:pwRANGE@127.0.0.1:99999/db"
    assert main(["rotate-master-key"], env=_env(DATABASE_URL=url)) == 1
    _assert_clean_failure(capsys, "pwRANGE")


def test_unknown_query_option_exits_1_with_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    url = "postgresql://u:pwOPT@127.0.0.1:1/db?bogusopt=1"
    assert main(["rotate-master-key"], env=_env(DATABASE_URL=url)) == 1
    _assert_clean_failure(capsys, "pwOPT")
