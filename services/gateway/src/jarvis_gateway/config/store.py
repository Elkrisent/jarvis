"""Holder of the live snapshot, swapped by a single reference assignment."""

from __future__ import annotations

import logging
from collections.abc import Callable

from jarvis_gateway.config.snapshot import Snapshot

log = logging.getLogger(__name__)


class ConfigStore:
    def __init__(self) -> None:
        self._current: Snapshot | None = None
        self._listeners: list[Callable[[Snapshot], None]] = []

    @property
    def current(self) -> Snapshot | None:
        return self._current

    def add_listener(self, listener: Callable[[Snapshot], None]) -> None:
        """Call ``listener`` with every snapshot installed from now on (and the current one)."""
        self._listeners.append(listener)
        if self._current is not None:
            self._notify(listener, self._current)

    def replace(self, snapshot: Snapshot) -> None:
        self._current = snapshot
        self._notify_all(snapshot)

    def update(self, fn: Callable[[Snapshot], Snapshot]) -> Snapshot:
        """Read-modify-replace with no await in between, so no update is lost."""
        current = self._current
        if current is None:
            raise RuntimeError("no config snapshot loaded")
        new = fn(current)
        self._current = new
        self._notify_all(new)
        return new

    def _notify_all(self, snapshot: Snapshot) -> None:
        for listener in self._listeners:
            self._notify(listener, snapshot)

    @staticmethod
    def _notify(listener: Callable[[Snapshot], None], snapshot: Snapshot) -> None:
        try:
            listener(snapshot)
        except Exception as exc:  # a listener must never break the swap; its message may hold data
            log.warning("a config listener failed (%s)", type(exc).__name__)
