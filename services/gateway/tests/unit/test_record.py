"""RequestRecord: exactly one event per request, never a failure for the caller."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from jarvis_core.memory import InMemoryTelemetrySink
from jarvis_core.telemetry import RequestEvent
from jarvis_gateway.record import RequestRecord

pytestmark = pytest.mark.unit


class Ticker:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def make(
    sink: InMemoryTelemetrySink | None = None, ticker: Ticker | None = None
) -> tuple[RequestRecord, InMemoryTelemetrySink, Ticker]:
    sink = sink or InMemoryTelemetrySink()
    ticker = ticker or Ticker()
    record = RequestRecord(
        sink,
        request_id="r1",
        method="POST",
        started=ticker.now,
        ts="2026-09-24T10:42:01.123Z",
        monotonic=ticker,
    )
    return record, sink, ticker


@pytest.mark.req("REQ-6.1-2")
def test_finish_emits_the_event_with_measured_numbers() -> None:
    record, sink, ticker = make()
    record.identify(project_id="p1", key_id="k1", api_id="a1", route="/v1/x/{id}")
    record.add_request_bytes(7)
    ticker.now += 0.010
    record.mark_attempt()
    ticker.now += 0.025
    record.mark_headers_received()
    record.add_response_bytes(100)
    record.add_response_bytes(20)
    ticker.now += 0.015
    record.finish(status=200, source="upstream", error_code=None)
    assert sink.events == [
        {
            "kind": "request",
            "v": 1,
            "request_id": "r1",
            "ts": "2026-09-24T10:42:01.123Z",
            "project_id": "p1",
            "key_id": "k1",
            "api_id": "a1",
            "method": "POST",
            "route": "/v1/x/{id}",
            "status": 200,
            "source": "upstream",
            "cache": None,
            "cache_key_hash": None,
            "attempts": 1,
            "latency_ms": 50,
            "upstream_latency_ms": 25,
            "req_bytes": 7,
            "resp_bytes": 120,
            "circuit": None,
            "error_code": None,
            "dedup_role": None,
        }
    ]


def test_an_attempt_without_headers_has_no_upstream_latency() -> None:
    record, sink, _ = make()
    record.mark_attempt()
    record.finish(status=504, source="jarvis", error_code="JV_UPSTREAM_TIMEOUT")
    assert sink.events[0]["attempts"] == 1
    assert sink.events[0]["upstream_latency_ms"] is None


def test_a_rejected_request_has_null_ids_and_no_attempt() -> None:
    record, sink, _ = make()
    record.finish(status=401, source="jarvis", error_code="JV_AUTH_INVALID")
    event = sink.events[0]
    assert (event["project_id"], event["key_id"], event["api_id"], event["route"]) == (
        None,
        None,
        None,
        None,
    )
    assert event["attempts"] == 0


@pytest.mark.req("REQ-6.3-4")
def test_finish_emits_once_and_later_calls_change_nothing() -> None:
    record, sink, _ = make()
    record.finish(status=200, source="upstream", error_code=None)
    record.finish(status=500, source="jarvis", error_code="JV_INTERNAL")
    record.add_response_bytes(5)
    assert len(sink.events) == 1
    assert sink.events[0]["status"] == 200
    assert sink.events[0]["resp_bytes"] == 0


class DroppingSink:
    dropped_total = 0

    def emit(self, event: Mapping[str, Any] | RequestEvent) -> bool:
        return False


class RaisingSink:
    dropped_total = 0

    def emit(self, event: Mapping[str, Any] | RequestEvent) -> bool:
        raise RuntimeError("sink exploded")


@pytest.mark.parametrize("sink", [DroppingSink(), RaisingSink()])
def test_a_failing_sink_never_reaches_the_caller(
    sink: Any, caplog: pytest.LogCaptureFixture
) -> None:
    record = RequestRecord(
        sink,
        request_id="r1",
        method="GET",
        started=0.0,
        ts="2026-01-01T00:00:00.000Z",
        monotonic=Ticker(),
    )
    record.finish(status=200, source="upstream", error_code=None)  # must not raise
    if isinstance(sink, RaisingSink):
        assert "r1" in caplog.text
        assert "sink exploded" not in caplog.text  # the exception text is not copied into logs


def test_invalid_values_are_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    record, sink, _ = make()
    record.finish(status=999, source="upstream", error_code=None)
    assert sink.events == []
    assert "r1" in caplog.text


def test_headers_without_an_attempt_leave_the_upstream_latency_unset() -> None:
    record, sink, _ = make()
    record.mark_headers_received()
    record.finish(status=401, source="jarvis", error_code="JV_AUTH_INVALID")
    assert sink.events[0]["upstream_latency_ms"] is None
