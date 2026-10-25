"""Config change events from Redis pub/sub, and the subscriber's reconnect loop.

Events are hints: the manager always re-reads the named rows from Postgres. Nothing from a payload
is ever logged (only the channel name, and only when it is one of ours), nor the Redis URL.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeGuard, cast, get_args

from jarvis_core.clock import Clock
from redis import asyncio as redis_asyncio

log = logging.getLogger(__name__)

CONFIG_CHANGED = "config:changed"
CONFIG_INVALIDATED = "config:invalidated"
CHANNELS = (CONFIG_CHANGED, CONFIG_INVALIDATED)

InvalidationKind = Literal["api", "key", "project"]
_KINDS: tuple[str, ...] = get_args(InvalidationKind)
_MAX_PAYLOAD_BYTES = 4096

_INITIAL_BACKOFF_S = 0.5
_MAX_BACKOFF_S = 5.0
_HEALTH_CHECK_INTERVAL_S = 5
_SOCKET_TIMEOUT_S = 5.0
_POLL_TIMEOUT_S = 1.0
# Polls (of up to _POLL_TIMEOUT_S each) to wait for both subscribe confirmations.
_CONFIRM_POLLS = 5


@dataclass(frozen=True)
class ApiChanged:
    api_id: str
    version: int


@dataclass(frozen=True)
class Invalidated:
    kind: InvalidationKind
    id: str


ConfigEvent = ApiChanged | Invalidated


def parse_event(channel: str | bytes, payload: bytes | str) -> ConfigEvent | None:
    """The event in ``payload``, or None for anything malformed (never raises)."""
    try:
        name = channel.decode() if isinstance(channel, bytes) else channel
        raw = payload.encode() if isinstance(payload, str) else payload
        if not isinstance(raw, bytes) or len(raw) > _MAX_PAYLOAD_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    if name == CONFIG_CHANGED:
        api_id, version = data.get("api_id"), data.get("version")
        if _id(api_id) and _version(version):
            return ApiChanged(api_id, version)
    elif name == CONFIG_INVALIDATED:
        kind, ident = data.get("kind"), data.get("id")
        if isinstance(kind, str) and kind in _KINDS and _id(ident):
            return Invalidated(cast(InvalidationKind, kind), ident)
    return None


def _reject_constant(name: str) -> object:
    raise ValueError("non-finite number")


def _id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and value != ""


def _version(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


class PubSubConnection(Protocol):
    async def subscribe(self, *channels: str) -> None: ...

    async def get_message(self, timeout: float) -> dict[str, Any] | None: ...

    async def aclose(self) -> None: ...


class _RedisPubSub:
    """One Redis client plus its pub/sub object, closed together."""

    def __init__(self, url: str) -> None:
        self._client = redis_asyncio.from_url(
            url,
            health_check_interval=_HEALTH_CHECK_INTERVAL_S,
            socket_connect_timeout=_SOCKET_TIMEOUT_S,
            socket_keepalive=True,
        )
        self._pubsub = self._client.pubsub()

    def __repr__(self) -> str:
        return "RedisPubSub()"

    async def subscribe(self, *channels: str) -> None:
        await self._pubsub.subscribe(*channels)

    async def get_message(self, timeout: float) -> dict[str, Any] | None:
        message: dict[str, Any] | None = await self._pubsub.get_message(
            ignore_subscribe_messages=False, timeout=timeout
        )
        return message

    async def aclose(self) -> None:
        try:
            await self._pubsub.aclose()  # type: ignore[no-untyped-call]  # untyped in redis-py
        finally:
            await self._client.aclose()


def connect_redis(url: str) -> PubSubConnection:
    return _RedisPubSub(url)


async def subscribe(
    redis_url: str,
    on_event: Callable[[ConfigEvent], Awaitable[None]],
    on_connected: Callable[[], Awaitable[None]],
    clock: Clock,
    stop: asyncio.Event,
    *,
    on_disconnected: Callable[[], None] | None = None,
    connect: Callable[[str], PubSubConnection] = connect_redis,
) -> None:
    """Listen on ``CHANNELS`` until ``stop`` is set, reconnecting forever.

    ``on_disconnected`` is called (synchronously, errors ignored) each time a connection attempt or
    a live connection fails, so the caller can poll faster while pushes are not arriving.

    ``on_connected`` runs once both subscriptions are confirmed, on every (re)connect, so the caller
    can resync whatever was missed while disconnected. Retries back off from 0.5 s, doubling to a
    5 s cap; a connection that stayed up for at least the cap resets the backoff. No error escapes
    (cancellation does).
    """
    backoff = _INITIAL_BACKOFF_S
    while not stop.is_set():
        conn: PubSubConnection | None = None
        up_since: float | None = None
        try:
            conn = connect(redis_url)
            await conn.subscribe(*CHANNELS)
            await _await_confirmations(conn, on_event)
            up_since = clock.monotonic()
            await _call(on_connected(), "resync after subscribe")
            while not stop.is_set():
                message = await conn.get_message(timeout=_POLL_TIMEOUT_S)
                if message is not None:
                    await _dispatch(message, on_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # Redis, socket or anything else: log the type, never the text
            if not stop.is_set():
                _notify(on_disconnected)
                log.warning(
                    "config subscriber lost Redis (%s); retrying in %.1f s",
                    type(exc).__name__,
                    _next_backoff(backoff, up_since, clock),
                )
        finally:
            if conn is not None:
                await _close(conn)
        if stop.is_set():
            return
        backoff = _next_backoff(backoff, up_since, clock)
        await _pause(clock, backoff, stop)
        backoff = min(backoff * 2, _MAX_BACKOFF_S)


def _notify(callback: Callable[[], None] | None) -> None:
    if callback is None:
        return
    try:
        callback()
    except Exception as exc:
        log.error("subscriber disconnect callback failed (%s)", type(exc).__name__)


def _next_backoff(backoff: float, up_since: float | None, clock: Clock) -> float:
    if up_since is not None and clock.monotonic() - up_since >= _MAX_BACKOFF_S:
        return _INITIAL_BACKOFF_S
    return backoff


async def _await_confirmations(
    conn: PubSubConnection, on_event: Callable[[ConfigEvent], Awaitable[None]]
) -> None:
    pending = set(CHANNELS)
    polls = 0
    while pending:
        message = await conn.get_message(timeout=_POLL_TIMEOUT_S)
        if message is None:
            polls += 1
            if polls >= _CONFIRM_POLLS:
                raise TimeoutError("no subscribe confirmation")
            continue
        if message.get("type") == "subscribe":
            pending.discard(_text(message.get("channel")))
        else:
            await _dispatch(message, on_event)


async def _dispatch(
    message: dict[str, Any], on_event: Callable[[ConfigEvent], Awaitable[None]]
) -> None:
    if message.get("type") != "message":
        return
    channel = _text(message.get("channel"))
    if channel not in CHANNELS:
        log.warning("ignoring a config event on an unexpected channel")
        return
    event = parse_event(channel, message.get("data", b""))
    if event is None:
        log.warning("ignoring a malformed config event on %s", channel)
        return
    await _call(on_event(event), "config event handler")


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


async def _call(awaitable: Awaitable[None], what: str) -> None:
    try:
        await awaitable
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error("%s failed (%s)", what, type(exc).__name__)


async def _close(conn: PubSubConnection) -> None:
    try:
        await conn.aclose()
    except asyncio.CancelledError:
        raise
    except Exception:  # closing a dead connection: nothing useful to report
        log.debug("closing the config subscriber connection failed")


async def _pause(clock: Clock, seconds: float, stop: asyncio.Event) -> None:
    """Sleep ``seconds`` on ``clock``, or less if ``stop`` is set meanwhile."""
    sleeper = asyncio.ensure_future(clock.sleep(seconds))
    stopper = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (sleeper, stopper):
            task.cancel()
        await asyncio.gather(sleeper, stopper, return_exceptions=True)
