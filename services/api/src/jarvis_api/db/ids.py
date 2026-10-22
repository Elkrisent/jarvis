"""Prefixed random identifiers, e.g. ``prj_3f2a…``."""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"
