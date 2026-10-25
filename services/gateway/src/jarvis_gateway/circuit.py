"""The gateway side of the circuit breaker (SPEC §4.8).

The transition rules are the pure state machine in ``jarvis_core.circuit``. This class holds a
local mirror of every circuit it has seen (refreshed every second and on pub/sub changes), so the
request path reads memory only, except for the one request that wins a half-open probe. Outcomes
are queued on the hot path and counted in the shared store by a background task.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

from jarvis_core.circuit import (
    admit,
    half_opened,
    probe_failed,
    probe_succeeded,
    tripped,
    window_s,
)
from jarvis_core.clock import Clock, SystemClock
from jarvis_core.config import CircuitPolicy
from jarvis_core.interfaces import CircuitSnapshot, CircuitState, CircuitStore, TelemetrySink
from jarvis_core.telemetry import SCHEMA_VERSION, format_ts

log = logging.getLogger(__name__)

QUEUE_LIMIT = 10_000
Listen = Callable[[Callable[[str], Awaitable[None]], asyncio.Event], Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class Ticket:
    """``allow``: go ahead. ``reject``: fail fast. ``probe``: go ahead and report the outcome."""

    action: Literal["allow", "probe", "reject"]
    state: CircuitState


class CircuitBreaker:
    def __init__(
        self,
        store: CircuitStore,
        *,
        clock: Clock | None = None,
        sink: TelemetrySink | None = None,
        publish: Callable[[str], Awaitable[None]] | None = None,
        listen: Listen | None = None,
        refresh_interval_s: float = 1.0,
    ) -> None:
        self._store = store
        self._clock = SystemClock() if clock is None else clock
        self._sink = sink
        self._publish = publish
        self._listen = listen
        self._refresh_interval_s = refresh_interval_s
        self._mirror: dict[str, CircuitSnapshot] = {}
        self._queue: deque[tuple[str, CircuitPolicy, str, bool]] = deque(maxlen=QUEUE_LIMIT)
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False

    # --- the request path -------------------------------------------------------------------

    def state(self, api_id: str) -> CircuitState:
        """The mirrored state, for ``X-Jarvis-Circuit`` and telemetry. No I/O."""
        return self._mirror.get(api_id, CircuitSnapshot()).state

    def is_open(self, api_id: str) -> bool:
        """True when the mirror says calls are not being let through (no I/O)."""
        return self.state(api_id) is not CircuitState.CLOSED

    async def admit(
        self, api_id: str, policy: CircuitPolicy, project_id: str, *, probe_ttl_s: int
    ) -> Ticket:
        """Decide whether this request may reach the upstream. Fails open on any internal error."""
        self._ensure_started()
        try:
            snapshot = self._mirror.get(api_id)
            if snapshot is None:
                snapshot = await self._refresh_one(api_id)
            decision = admit(policy, snapshot, self._clock.now())
            if decision.action == "allow":
                return Ticket("allow", CircuitState.CLOSED)
            if decision.action == "reject":
                return Ticket("reject", decision.state)
            return await self._try_probe(api_id, policy, project_id, probe_ttl_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "circuit check failed (%s); letting the request through", type(exc).__name__
            )
            return Ticket("allow", CircuitState.CLOSED)

    def record(self, api_id: str, policy: CircuitPolicy, project_id: str, *, failure: bool) -> None:
        """Queue one call outcome. O(1), no await, no I/O."""
        self._ensure_started()
        self._queue.append((api_id, policy, project_id, failure))
        self._wake.set()

    async def finish_probe(
        self,
        api_id: str,
        policy: CircuitPolicy,
        project_id: str,
        outcome: Literal["success", "failure", "cancelled"],
    ) -> None:
        """Settle the half-open probe: close, reopen for longer, or just give the slot back."""
        await asyncio.shield(self._finish_probe(api_id, policy, project_id, outcome))

    # --- lifecycle --------------------------------------------------------------------------

    async def drain(self) -> None:
        """Count every queued outcome now (the background task does this continuously)."""
        while self._queue:
            await self._count(*self._queue.popleft())

    async def refresh(self) -> None:
        for api_id in list(self._mirror):
            await self._refresh_one(api_id)

    async def on_remote_change(self, api_id: str) -> None:
        await self._refresh_one(api_id)

    async def aclose(self) -> None:
        self._stop.set()
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        try:
            async with asyncio.timeout(1.0):
                await self.drain()
        except Exception as exc:
            log.debug("circuit close: %s", type(exc).__name__)
        closer = getattr(self._store, "aclose", None)
        if closer is not None:
            await closer()

    # --- internals --------------------------------------------------------------------------

    def _ensure_started(self) -> None:
        if self._started or self._stop.is_set():
            return
        self._started = True
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._drain_loop()))
        self._tasks.append(loop.create_task(self._refresh_loop()))
        if self._listen is not None:
            self._tasks.append(loop.create_task(self._listen(self.on_remote_change, self._stop)))

    async def _drain_loop(self) -> None:
        while True:  # ends by cancellation in aclose
            await self._wake.wait()
            self._wake.clear()
            try:
                await self.drain()
            except Exception as exc:
                log.warning("circuit outcome counting failed (%s)", type(exc).__name__)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_interval_s)
            try:
                await self.refresh()
            except Exception as exc:
                log.warning("circuit refresh failed (%s)", type(exc).__name__)

    async def _refresh_one(self, api_id: str) -> CircuitSnapshot:
        snapshot = await self._store.get(api_id)
        self._mirror[api_id] = snapshot
        return snapshot

    async def _count(
        self, api_id: str, policy: CircuitPolicy, project_id: str, failure: bool
    ) -> None:
        snapshot = self._mirror.get(api_id, CircuitSnapshot())
        if snapshot.state is not CircuitState.CLOSED:
            return  # nothing is being let through; stragglers do not count
        now = self._clock.now()
        stats = await self._store.record_call(
            api_id, success=not failure, window_s=window_s(policy, snapshot, now)
        )
        opened = tripped(policy, snapshot, stats, now)
        if opened is None:
            return
        current = await self._store.get(api_id)  # another instance may have tripped it already
        if current.state is not CircuitState.CLOSED:
            self._mirror[api_id] = current
            return
        await self._move(api_id, project_id, current, opened, "failure_rate")

    async def _try_probe(
        self, api_id: str, policy: CircuitPolicy, project_id: str, probe_ttl_s: int
    ) -> Ticket:
        if not await self._store.try_acquire_probe(api_id, ttl_s=probe_ttl_s):
            return Ticket("reject", CircuitState.HALF_OPEN)
        current = await self._refresh_one(api_id)
        again = admit(policy, current, self._clock.now())
        if again.action != "probe":  # someone else settled it while we took the lock
            await self._store.release_probe(api_id)
            return Ticket(again.action, again.state)
        if current.state is CircuitState.OPEN:
            await self._move(api_id, project_id, current, half_opened(current), "probe")
        return Ticket("probe", CircuitState.HALF_OPEN)

    async def _finish_probe(
        self,
        api_id: str,
        policy: CircuitPolicy,
        project_id: str,
        outcome: Literal["success", "failure", "cancelled"],
    ) -> None:
        try:
            if outcome == "cancelled":
                return
            current = await self._store.get(api_id)
            now = self._clock.now()
            if outcome == "success":
                await self._move(api_id, project_id, current, probe_succeeded(now), "probe_ok")
            else:
                await self._move(
                    api_id, project_id, current, probe_failed(policy, current, now), "probe_failed"
                )
        finally:
            await self._store.release_probe(api_id)

    async def _move(
        self,
        api_id: str,
        project_id: str,
        old: CircuitSnapshot,
        new: CircuitSnapshot,
        reason: str,
    ) -> None:
        await self._store.set(api_id, new)
        self._mirror[api_id] = new
        self._emit(api_id, project_id, old, new, reason)
        if self._publish is not None:
            try:
                await self._publish(api_id)
            except Exception as exc:  # the 1 s refresh covers a missed push
                log.debug("circuit publish failed (%s)", type(exc).__name__)

    def _emit(
        self, api_id: str, project_id: str, old: CircuitSnapshot, new: CircuitSnapshot, reason: str
    ) -> None:
        if self._sink is None:
            return
        event: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "kind": "event",
            "type": "circuit_transition",
            "ts": format_ts(self._clock.now()),
            "project_id": project_id,
            "api_id": api_id,
            "from": old.state.value,
            "to": new.state.value,
            "open_duration_s": new.open_duration_s,
            "reason": reason,
        }
        self._sink.emit(event)
