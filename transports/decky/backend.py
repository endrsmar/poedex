"""The runtime, hosted inside a Decky plugin process.

## What this is

Decky Loader forks a process per plugin and imports its `main.py`. That process has
its own asyncio loop running `run_forever()`; `_main()` is scheduled as a task and
**not awaited**, so a long-running task there is the intended pattern rather than a
hack (research-notes §5, read from the loader's source). The process survives the
panel closing, the game launching and exiting, and a Steam UI reload. It stops on
Decky shutdown, on disable/uninstall, or on a debug-flag hot reload.

So this class is the plugin's whole backend: it starts the module registry once,
serves method calls from the panel, forwards runtime events to the frontend with
`decky.emit()`, and shuts down fast when asked.

## Four things it does that the HTTP transport does not have to

* **One RPC entry point, not one per method.** Decky's `callable(route)(...args)`
  sends *positional* arguments to a same-named coroutine on the `Plugin` class,
  which does not fit a registry of ~20 methods with keyword arguments. So the
  frontend calls one method — `call(method, params)` — and this hands it straight to
  `transports.dispatch.call_method`, the same function the FastAPI route calls.
  **Dispatch is not duplicated**, which is the point: `FORBIDDEN_METHODS`, the
  redaction and the structured errors are one implementation with two doors.

* **It pushes.** `decky.emit()` means the panel can open already populated instead of
  fetching on open (SPEC §6.2). Every runtime event is forwarded, and the last one
  per topic is kept so a panel that mounts late can ask for the current state rather
  than wait for the next change.

* **It watches the clock rather than trusting a sleep.** Nothing in Decky Loader
  handles suspend/resume, so `asyncio.sleep(60)` across a lid close returns whenever
  the kernel gets round to it. :meth:`_heartbeat` therefore sleeps in short steps and
  compares `time.monotonic()` deltas; a jump larger than
  :data:`SUSPEND_JUMP_SECONDS` is treated as *resumed* — the caches are marked stale
  and a `resumed` event goes to the panel, so the first thing the player sees after
  opening the lid is not a price from before lunch. **Unverified on hardware**;
  `docs/deck-checklist.md` item 9.

* **It shuts down against a deadline.** Decky sends SIGTERM and SIGKILLs five seconds
  later. :meth:`shutdown` gives the registry :data:`SHUTDOWN_BUDGET_SECONDS` and then
  stops waiting — an unclean stop is better than being killed mid-write, and the
  storage layer's writes are atomic precisely so that this is survivable.

## What is not here

No `root`. No path outside `DECKY_PLUGIN_SETTINGS_DIR` / `DECKY_PLUGIN_RUNTIME_DIR`,
which `runtime/storage.py` already reads. And no way to reach the credential: the
frontend goes through the same `FORBIDDEN_METHODS` list the HTTP transport does.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from runtime.events import Event
from runtime.log import get_logger, install_redaction, silence_noisy_loggers
from runtime.registry import Registry, discover
from transports.dispatch import call_method, exposed_methods, server_meta

__all__ = [
    "HEARTBEAT_SECONDS",
    "SHUTDOWN_BUDGET_SECONDS",
    "SUSPEND_JUMP_SECONDS",
    "VERSION",
    "DeckyBackend",
    "install_decky_logging",
]

VERSION = "0.1.0"

HEARTBEAT_SECONDS = 5.0
"""How often the clock is checked. Short enough that a suspend is noticed before the
player has finished looking at the panel, long enough to be free."""

SUSPEND_JUMP_SECONDS = 90.0
"""A monotonic gap larger than this is a suspend, not a slow loop.

`time.monotonic()` on Linux is `CLOCK_MONOTONIC`, which **does not advance across
suspend** — so the observed symptom of a resume is that a 5-second sleep appears to
have taken 5 seconds while the wall clock moved an hour. Both clocks are therefore
compared, and the threshold is on the *difference* between them."""

SHUTDOWN_BUDGET_SECONDS = 3.0
"""Decky SIGKILLs 5 s after SIGTERM. Three leaves room for the loop to unwind."""

RESUMED = "system.resumed"
"""Event topic. Payload: how long the Deck was away, in seconds."""

_log = get_logger("transport.decky")


def install_decky_logging(logger: logging.Logger | None = None) -> None:
    """Make Decky's logging safe to use.

    `decky.logger` reconfigures the **root** logger with ``force=True`` (SPEC §8), so
    every handler this project attached is replaced underneath it and `httpx` — which
    logs request URLs at DEBUG — starts writing into the plugin log file. This is
    called *after* `import decky`, and re-attaches both halves: the redacting filter
    to whatever handlers now exist, and the level floor on the libraries that talk.
    """
    install_redaction(logger)
    silence_noisy_loggers()


class DeckyBackend:
    """The registry, an event fan-out, and a shutdown that finishes on time.

    `emit` is injected rather than imported so this class never has to see `decky`;
    `plugin/main.py` passes `decky.emit`. That is what makes the whole transport
    testable on a laptop, and it is also what keeps `transports/decky` importable by
    `tests/`, which has no Decky Loader to import from.
    """

    def __init__(
        self,
        *,
        emit: Callable[[str, Any], Awaitable[None]] | None = None,
        modules_root: Path | str | None = None,
        registry: Registry | None = None,
    ) -> None:
        self._emit = emit
        self._modules_root = modules_root
        self._registry: Registry | None = registry
        self._unsubscribe: Callable[[], None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._latest: dict[str, dict[str, Any]] = {}
        self._started = False
        self._resumes = 0

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Build and start the registry, then begin forwarding events.

        Called from `_main()`, which Decky schedules and does not await.
        """
        if self._started:
            return
        registry = self._registry
        if registry is None:
            registry = Registry()
            registry.register_all(discover(self._modules_root or _default_modules_root()))
            self._registry = registry
        # Subscribed *before* start_all, so a module that emits during startup — the
        # price-table prefetch does — is not a push the panel silently missed.
        self._unsubscribe = registry.events.subscribe("*", self._forward)
        await registry.start_all()
        self._started = True
        self._heartbeat = asyncio.get_running_loop().create_task(self._watch_the_clock())
        _log.info("poedex backend up: %d method(s)", len(exposed_methods(registry)))
        await self._push("backend.ready", self.meta())

    async def shutdown(self) -> None:
        """Stop, within :data:`SHUTDOWN_BUDGET_SECONDS`.

        `_unload` has five seconds before SIGKILL. A module that hangs on a socket
        must not take the rest down with it, and every write this project makes is
        atomic (`runtime/storage.py`), so an unclean stop loses at most an in-flight
        cache refresh.
        """
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat
            self._heartbeat = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        registry = self._registry
        self._started = False
        if registry is None:
            return
        try:
            await asyncio.wait_for(registry.stop_all(), timeout=SHUTDOWN_BUDGET_SECONDS)
        except (TimeoutError, asyncio.CancelledError):
            _log.warning(
                "shutdown gave up after %.0fs; Decky SIGKILLs at 5s and every write is atomic",
                SHUTDOWN_BUDGET_SECONDS,
            )
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("shutdown raised %s", type(exc).__name__)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def registry(self) -> Registry:
        if self._registry is None:
            raise RuntimeError("the poedex backend has not been started")
        return self._registry

    # -- what the frontend calls ----------------------------------------------

    async def call(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """One method call, as a plain JSON-able envelope.

        The envelope rather than a bare result, because Decky's RPC turns an
        exception into a string in the CEF console and this project's errors carry
        structure a panel acts on — `kind`, and `retry_after`, which is what runs the
        countdown on the refresh button instead of a red box that says "try again"
        while every attempt is refused.

        A backend that has not finished starting answers rather than raising: the
        panel can open before `_main()` has got through the poe.ninja prefetch.
        """
        if not self._started or self._registry is None:
            # Same five keys as every other reply. A frontend that had to check
            # whether `retry_after` was present before reading it would get that
            # wrong on the one path nobody exercises.
            return {
                "ok": False,
                "result": None,
                "error": {
                    "kind": "NotStartedError",
                    "message": "the poedex backend is still starting",
                    "retry_after": 1.0,
                },
                "status": 503,
                "retry_after": 1.0,
            }
        result = await call_method(self._registry, method, params, version=VERSION)
        return {
            "ok": result.ok,
            "result": result.result,
            "error": result.error,
            "status": result.status,
            "retry_after": result.retry_after,
        }

    def meta(self) -> dict[str, Any]:
        """Modules, their state and their reason — the compact profile's `_server.meta`."""
        if self._registry is None:
            return {"version": VERSION, "profile": "compact", "modules": [], "methods": []}
        return server_meta(self._registry, version=VERSION, profile="compact")

    def latest(self, topic: str | None = None) -> dict[str, Any]:
        """The last payload seen per topic.

        Panel content is unmounted whenever the QAM closes (SPEC §6.2), so a screen
        that mounts after an event has already fired would otherwise wait for the
        next one. This is the module-level store that pattern asks for, kept on the
        backend side where it survives everything.
        """
        if topic is not None:
            return self._latest.get(topic, {})
        return dict(self._latest)

    # -- events ----------------------------------------------------------------

    def _forward(self, event: Event) -> None:
        """Runtime bus -> `decky.emit`.

        The bus calls handlers synchronously, so the push is scheduled rather than
        awaited. A failing push must not stop the emitter — an event is a
        notification, not a call, which is the rule the bus itself follows.
        """
        payload = {
            "topic": event.topic,
            "payload": dict(event.payload),
            "source": event.source,
            "at": event.at,
        }
        self._latest[event.topic] = payload
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self._deliver(payload))

    async def _push(self, topic: str, payload: dict[str, Any]) -> None:
        message = {"topic": topic, "payload": payload, "source": "transport", "at": time.time()}
        self._latest[topic] = message
        await self._deliver(message)

    async def _deliver(self, message: dict[str, Any]) -> None:
        if self._emit is None:
            return
        try:
            await self._emit("poedex", message)
        except Exception as exc:  # pragma: no cover - the CEF side went away
            _log.debug("emit failed: %s", type(exc).__name__)

    # -- suspend/resume --------------------------------------------------------

    async def _watch_the_clock(self) -> None:
        """Notice that the Deck was asleep.

        Nothing in the loader handles suspend/resume, so this is the only thing that
        will. `CLOCK_MONOTONIC` stops during suspend on Linux and the wall clock does
        not, so the two drifting apart *is* the resume signal — which is a stronger
        test than "that sleep took too long", since a stalled loop moves neither.

        On resume everything is stale by definition: an item cache from before the
        lid closed, a price table from before, a rate-limit budget learned from
        headers that have since expired. So the panel is told, and it is told with a
        number, because "the Deck was away for 4 hours" is a different sentence from
        "the Deck was away for 40 seconds".
        """
        monotonic = time.monotonic()
        wall = time.time()
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            now_monotonic, now_wall = time.monotonic(), time.time()
            slept = now_monotonic - monotonic
            elapsed = now_wall - wall
            monotonic, wall = now_monotonic, now_wall
            gap = elapsed - slept
            if gap < SUSPEND_JUMP_SECONDS:
                continue
            self._resumes += 1
            _log.info("resumed after about %.0fs away", gap)
            await self._push(RESUMED, {"away_seconds": round(gap, 1), "count": self._resumes})

    @property
    def resumes(self) -> int:
        """How many suspends this process has seen. For the checklist, and for tests."""
        return self._resumes


def _default_modules_root() -> Path:
    """Where `modules/` sits inside an installed plugin.

    `plugin/main.py` lives at the plugin root with `modules/` beside it, so this is
    one directory up from `transports/`. Resolved from `__file__` rather than from a
    `DECKY_PLUGIN_DIR` lookup so the same code runs from a source checkout.
    """
    return Path(__file__).resolve().parents[2] / "modules"
