"""The `gamelog` core module.

The second of the tool's two inputs (CLAUDE.md): a **read-only tail** of the log
Path of Exile writes to the Linux filesystem. It never touches the game process, and
GGG's terms carve out tools that *"read the client log files"* — this is the safe
half of the design, and it is what lets us delete interval polling of the API, which
is the risky half.

It is *core* because it holds no feature opinion. It reports that a zone was entered
and what kind of area it was. Whether that should trigger a sync, whether a map entry
should be skipped because the bag was just emptied, and the 20-second debounce are
all SPEC §4.4 policy and belong to the consumer.

Two environment variables exist as development affordances and are documented here
rather than hidden:

``POEDEX_GAMELOG_PATH``
    Overrides the resolved log path, ahead of the ``log_path`` setting. This is how
    ``poedex gamelog watch --path`` works and how the test suite stays off the real
    ``~/.steam``.
``POEDEX_GAMELOG_FROM_START``
    Reads an existing log from byte 0 instead of seeking to EOF. Debug only —
    "never read from byte 0" is the product behaviour, and this exists so the
    behaviour can be exercised against a synthetic file.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from modules.gamelog.backend.api import (
    FROM_START_ENV,
    GAMELOG_AVAILABLE,
    GAMELOG_UNAVAILABLE,
    PATH_ENV,
    ZONE_ENTERED,
    GameLogApi,
    GameLogStatus,
    LogState,
    ZoneEvent,
)
from modules.gamelog.backend.locate import LogLocation
from modules.gamelog.backend.parse import PAIR_WINDOW, ZoneTracker, compile_patterns
from modules.gamelog.backend.tailer import (
    POLL_INTERVAL,
    RESOLVE_INTERVAL,
    LogWatcher,
    default_resolver,
)
from runtime.context import ModuleContext


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class GameLogModule:
    id = "gamelog"
    name = "Game log"
    kind = "core"
    requires: ClassVar[list[str]] = []
    provides: type | None = GameLogApi

    def __init__(self, watcher: LogWatcher | None = None) -> None:
        # An injectable watcher keeps the tests off the real filesystem and off the
        # real clock.
        self._injected = watcher
        self._watcher: LogWatcher | None = None
        self._tracker: ZoneTracker | None = None
        self._ctx: ModuleContext | None = None
        self._last_event: ZoneEvent | None = None
        self._pending_emissions: list[ZoneEvent] = []
        self._pending_state: tuple[LogState, LogLocation | None, str | None] | None = None
        self._announced: LogState | None = None

    # -- lifecycle -------------------------------------------------------------

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        self._tracker = ZoneTracker(
            entered_patterns=compile_patterns(ctx.settings.get("entered_patterns", [])),
            pair_window=float(ctx.settings.get("pair_window_seconds", PAIR_WINDOW)),
        )
        self._watcher = self._injected or self._build_watcher(ctx)
        self._watcher.on_lines = self._on_lines
        self._watcher.on_state = self._on_state
        self._watcher.on_tick = self._on_tick
        self._watcher.start()
        ctx.logger.info("gamelog watching; poll=%ss", self._watcher.poll_interval)

    def _build_watcher(self, ctx: ModuleContext) -> LogWatcher:
        override = os.environ.get(PATH_ENV) or (ctx.settings.get("log_path", "") or None)
        return LogWatcher(
            default_resolver(override=override),
            on_lines=self._on_lines,
            on_state=self._on_state,
            on_tick=self._on_tick,
            poll_interval=float(ctx.settings.get("poll_interval_seconds", POLL_INTERVAL)),
            resolve_interval=float(ctx.settings.get("resolve_interval_seconds", RESOLVE_INTERVAL)),
            from_start=_truthy(os.environ.get(FROM_START_ENV)),
        )

    async def stop(self) -> None:
        if self._watcher is not None:
            await self._watcher.stop()
        self._watcher = None
        self._tracker = None
        self._ctx = None

    def methods(self) -> dict[str, Callable[..., Any]]:
        return {
            "status": self.status_json,
            "resolve": self.resolve_json,
        }

    def settings_schema(self) -> dict[str, Any]:
        return {
            "log_path": {
                "type": "str",
                "default": "",
                "label": "Client.txt path",
                "description": (
                    "Manual override. Leave empty to find it through Steam's "
                    "libraryfolders.vdf. Set this when the game lives somewhere "
                    "the automatic probe does not look."
                ),
            },
            "poll_interval_seconds": {
                "type": "float",
                "default": POLL_INTERVAL,
                "min": 0.2,
                "max": 60.0,
                "label": "Poll interval",
                "description": "How often the log's size is checked. One stat() each.",
            },
            "resolve_interval_seconds": {
                "type": "float",
                "default": RESOLVE_INTERVAL,
                "min": 1.0,
                "max": 3600.0,
                "label": "Re-probe interval",
                "description": (
                    "How often to look for the log again while it is missing. Slow "
                    "on purpose: the file does not exist until the game has run once."
                ),
            },
            "pair_window_seconds": {
                "type": "float",
                "default": PAIR_WINDOW,
                "min": 0.0,
                "max": 30.0,
                "label": "Area/name pairing window",
                "description": (
                    "How long a generated area waits for its 'you have entered' line "
                    "before being reported without a display name."
                ),
            },
            "entered_patterns": {
                "type": "list",
                "default": [],
                "label": "Extra zone-entry phrases",
                "description": (
                    "Regexes matching your client's localized 'You have entered X.' "
                    "message, capturing the area as (?P<name>...). Only English ships "
                    "verified; without a match, zone changes are still detected from "
                    "the language-independent area id."
                ),
            },
        }

    # -- GameLogApi ------------------------------------------------------------

    async def status(self) -> GameLogStatus:
        watcher = self._watcher
        if watcher is None:
            return GameLogStatus(state=LogState.STOPPED, detail="module is not started")
        return self._status_of(watcher.state, watcher.location, watcher.detail, watcher.searched)

    async def path(self) -> str | None:
        watcher = self._watcher
        if watcher is None or watcher.location is None:
            return None
        return str(watcher.location.path)

    async def last_event(self) -> ZoneEvent | None:
        return self._last_event

    async def resolve(self) -> GameLogStatus:
        watcher = self._watcher
        if watcher is None:
            return GameLogStatus(state=LogState.STOPPED, detail="module is not started")
        await watcher.resolve()
        await self._flush_state()
        await self._flush_events()
        return await self.status()

    # -- JSON wrappers for the method registry ---------------------------------

    async def status_json(self) -> dict[str, Any]:
        return (await self.status()).to_json()

    async def resolve_json(self) -> dict[str, Any]:
        return (await self.resolve()).to_json()

    # -- watcher callbacks -----------------------------------------------------
    #
    # The follower is synchronous, so these buffer and the async tick drains them.
    # Emitting from inside a sync callback would need a task per line.

    def _on_lines(self, lines: Sequence[str]) -> None:
        tracker = self._tracker
        if tracker is None:  # pragma: no cover - stopped mid-poll
            return
        for line in lines:
            self._pending_emissions.extend(tracker.feed(line))

    def _on_state(
        self,
        state: LogState,
        location: LogLocation | None,
        detail: str | None,
        searched: Sequence[LogLocation],
    ) -> None:
        self._pending_state = (state, location, detail)

    async def _on_tick(self) -> None:
        if self._tracker is not None:
            self._pending_emissions.extend(self._tracker.flush())
        await self._flush_state()
        await self._flush_events()

    async def _flush_events(self) -> None:
        events, self._pending_emissions = self._pending_emissions, []
        for event in events:
            self._last_event = event
            if self._ctx is None:  # pragma: no cover - stopped mid-flush
                continue
            self._ctx.logger.info(
                "zone entered: kind=%s id=%s name=%s",
                event.kind.value,
                event.area_id,
                event.name,
            )
            await self._ctx.events.emit(ZONE_ENTERED, event.to_json(), source=self.id)

    async def _flush_state(self) -> None:
        pending, self._pending_state = self._pending_state, None
        if pending is None or self._ctx is None:
            return
        state, location, detail = pending
        if state is self._announced:
            return
        self._announced = state
        status = self._status_of(
            state, location, detail, self._watcher.searched if self._watcher else ()
        )
        if state is LogState.WATCHING:
            self._ctx.logger.info("gamelog available: %s", status.path)
            await self._ctx.events.emit(GAMELOG_AVAILABLE, status.to_json(), source=self.id)
            return
        if state is LogState.STOPPED:
            return
        # SPEC §4.6: never degrade silently.
        self._ctx.logger.warning("gamelog unavailable (%s): %s", state.value, detail)
        await self._ctx.events.emit(GAMELOG_UNAVAILABLE, status.to_json(), source=self.id)

    def _status_of(
        self,
        state: LogState,
        location: LogLocation | None,
        detail: str | None,
        searched: Sequence[LogLocation],
    ) -> GameLogStatus:
        return GameLogStatus(
            state=state,
            path=str(location.path) if location else None,
            origin=location.origin if location else None,
            detail=detail,
            last_event=self._last_event,
            searched=tuple(str(c.path) for c in searched),
        )

    def __repr__(self) -> str:
        return f"GameLogModule(state={self._watcher.state.value if self._watcher else 'stopped'})"


# The registry discovers modules by importing this file and reading MODULE.
MODULE = GameLogModule()
