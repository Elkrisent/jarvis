"""Test doubles for the api service."""

from __future__ import annotations


class RecordingPublisher:
    """A ConfigPublisher that records calls instead of talking to Redis."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def config_changed(self, api_id: str, version: int) -> None:
        self.calls.append((api_id, version))
