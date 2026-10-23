"""Shared api-test setup."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

# Expose tests/fakes.py as the top-level module ``fakes`` (no package, importlib import mode).
sys.path.insert(0, str(Path(__file__).parent))

# create_app_from_env validates the full Settings, so env-based startup tests need these two.
_ADMIN_TOKEN = "a" * 32
_MASTER_KEY = base64.b64encode(bytes(range(32))).decode()


@pytest.fixture(autouse=True)
def _startup_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_ADMIN_TOKEN", _ADMIN_TOKEN)
    monkeypatch.setenv("JARVIS_MASTER_KEY", _MASTER_KEY)
