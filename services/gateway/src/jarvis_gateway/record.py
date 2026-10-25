"""What the gateway knows about one request, turned into exactly one telemetry event.

Created at ingress, filled in as the request moves through the pipeline, finished once when the
response is complete. Emitting is fire-and-forget (SPEC §6.3): nothing here can fail the request.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from jarvis_core.interfaces import TelemetrySink
from jarvis_core.telemetry import event_from_outcome

log = logging.getLogger(__name__)


class RequestRecord:
    def __init__(
        self,
        sink: TelemetrySink,
        *,
        request_id: str,
        method: str,
        started: float,
        ts: str,
        monotonic: Callable[[], float],
    ) -> None:
        self._sink = sink
        self._request_id = request_id
        self._method = method
        self._started = started
        self._ts = ts
        self._monotonic = monotonic
        self._project_id: str | None = None
        self._key_id: str | None = None
        self._api_id: str | None = None
        self._route: str | None = None
        self._attempts = 0
        self._attempt_started: float | None = None
        self._upstream_ms: int | None = None
        self._req_bytes = 0
        self._resp_bytes = 0
        self._circuit: str | None = None
        self._finished = False

    def identify(self, *, project_id: str, key_id: str, api_id: str, route: str | None) -> None:
        self._project_id, self._key_id, self._api_id, self._route = (
            project_id,
            key_id,
            api_id,
            route,
        )

    def set_circuit(self, state: str) -> None:
        self._circuit = state

    def mark_attempt(self) -> None:
        self._attempts += 1
        self._attempt_started = self._monotonic()

    def mark_headers_received(self) -> None:
        if self._attempt_started is not None:
            self._upstream_ms = _ms(self._monotonic() - self._attempt_started)

    def add_request_bytes(self, count: int) -> None:
        self._req_bytes += count

    def add_response_bytes(self, count: int) -> None:
        if not self._finished:
            self._resp_bytes += count

    def finish(self, *, status: int, source: str, error_code: str | None) -> None:
        """Emit the event. Only the first call has an effect."""
        if self._finished:
            return
        self._finished = True
        try:
            event = event_from_outcome(
                request_id=self._request_id,
                ts=self._ts,
                project_id=self._project_id,
                key_id=self._key_id,
                api_id=self._api_id,
                method=self._method,
                route=self._route,
                status=status,
                source=source,
                attempts=self._attempts,
                latency_ms=_ms(self._monotonic() - self._started),
                upstream_latency_ms=self._upstream_ms,
                req_bytes=self._req_bytes,
                resp_bytes=self._resp_bytes,
                error_code=error_code,
                circuit=self._circuit,
            )
            self._sink.emit(event)
        except Exception:
            # The request already has its answer; a telemetry fault must not change it.
            log.warning("telemetry event for request %s was not emitted", self._request_id)


def _ms(seconds: float) -> int:
    return max(0, int(seconds * 1000))
