"""Holder of the live snapshot, swapped by a single reference assignment."""

from __future__ import annotations

from collections.abc import Callable

from jarvis_gateway.config.snapshot import Snapshot


class ConfigStore:
    def __init__(self) -> None:
        self._current: Snapshot | None = None

    @property
    def current(self) -> Snapshot | None:
        return self._current

    def replace(self, snapshot: Snapshot) -> None:
        self._current = snapshot

    def update(self, fn: Callable[[Snapshot], Snapshot]) -> Snapshot:
        """Read-modify-replace with no await in between, so no update is lost."""
        current = self._current
        if current is None:
            raise RuntimeError("no config snapshot loaded")
        new = fn(current)
        self._current = new
        return new
