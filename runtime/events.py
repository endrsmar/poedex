"""Async event bus.

Topics are dotted strings (``credential_changed``, ``sync_complete``,
``zone_changed``). A subscriber may register for an exact topic, a ``prefix.*``
pattern, or ``*`` for everything.

A handler that raises does not stop the other handlers and does not propagate to the
emitter: an event is a notification, not a call. Failures are logged (redacted) and
counted so tests and the UI can see that something is wrong.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from runtime.log import get_logger

Handler = Callable[["Event"], Awaitable[None] | None]
Unsubscribe = Callable[[], None]

_log = get_logger("runtime.events")


@dataclass(frozen=True, slots=True)
class Event:
    topic: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    source: str | None = None
    at: float = field(default_factory=time.time)


def _matches(pattern: str, topic: str) -> bool:
    if pattern == "*" or pattern == topic:
        return True
    if pattern.endswith(".*"):
        return topic.startswith(pattern[:-1])
    return False


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self._waiters: list[tuple[str, asyncio.Future[Event]]] = []
        self.handler_errors = 0

    def subscribe(self, pattern: str, handler: Handler) -> Unsubscribe:
        """Register ``handler`` for ``pattern``. Returns a callable that removes it."""
        self._handlers.setdefault(pattern, []).append(handler)

        def _unsubscribe() -> None:
            self.unsubscribe(pattern, handler)

        return _unsubscribe

    def unsubscribe(self, pattern: str, handler: Handler) -> None:
        handlers = self._handlers.get(pattern)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            return
        if not handlers:
            del self._handlers[pattern]

    def unsubscribe_all(self, handler_owner: object | None = None) -> None:
        """Drop every subscription, or every one whose handler is bound to an owner."""
        if handler_owner is None:
            self._handlers.clear()
            return
        for pattern in list(self._handlers):
            kept = [
                h for h in self._handlers[pattern]
                if getattr(h, "__self__", None) is not handler_owner
            ]
            if kept:
                self._handlers[pattern] = kept
            else:
                del self._handlers[pattern]

    def subscriber_count(self, topic: str) -> int:
        return sum(len(hs) for p, hs in self._handlers.items() if _matches(p, topic))

    async def emit(
        self,
        topic: str,
        payload: Mapping[str, Any] | None = None,
        *,
        source: str | None = None,
    ) -> Event:
        """Deliver an event to every matching handler and return it."""
        event = Event(topic=topic, payload=dict(payload or {}), source=source)
        self._resolve_waiters(event)

        handlers = [h for p, hs in self._handlers.items() if _matches(p, topic) for h in hs]
        if not handlers:
            return event

        results = await asyncio.gather(
            *(self._invoke(h, event) for h in handlers), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                self.handler_errors += 1
                _log.exception(
                    "event handler failed for topic %s", topic, exc_info=result
                )
        return event

    async def _invoke(self, handler: Handler, event: Event) -> None:
        result = handler(event)
        if inspect.isawaitable(result):
            await result

    async def wait_for(self, pattern: str, timeout: float = 5.0) -> Event:
        """Await the next event matching ``pattern``. Mostly a testing affordance."""
        future: asyncio.Future[Event] = asyncio.get_running_loop().create_future()
        entry = (pattern, future)
        self._waiters.append(entry)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            if entry in self._waiters:
                self._waiters.remove(entry)

    def _resolve_waiters(self, event: Event) -> None:
        for entry in list(self._waiters):
            pattern, future = entry
            if _matches(pattern, event.topic) and not future.done():
                future.set_result(event)
                self._waiters.remove(entry)
