"""The public surface of the `gamelog` module.

**This is the only file in this module that other modules may import** (plan §1.4,
enforced by ``tests/test_boundaries.py``). Everything here is a Protocol, a plain
data type, or an enum — no implementation, no state, no imports from the rest of
the module.

What this module reports is *what the log said*. It holds no sync policy: the 20 s
zone-entry debounce and the "skip maps, the bag was just emptied" rule of SPEC §4.4
are the consumer's decisions, not this module's. A core module with a feature
opinion is how a contained core stops being contained (plan §1.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

ZONE_ENTERED = "gamelog.zone_entered"
"""Emitted once per detected zone entry. Payload is :meth:`ZoneEvent.to_json`."""

GAMELOG_UNAVAILABLE = "gamelog.unavailable"
"""Emitted when the watcher cannot currently observe the log — the path could not
be resolved, or it resolved to a file that does not exist yet. Payload is
:meth:`GameLogStatus.to_json`.

SPEC §4.6: *"if the path cannot be resolved, degrade to button + QAM-open and say so
in the panel. Never degrade silently."* This event is that signal."""

GAMELOG_AVAILABLE = "gamelog.available"
"""The counterpart: the watcher is now following a real file. Emitted on the
transition only, so a surface showing the degraded banner knows to take it down."""

PATH_ENV = "POEDEX_GAMELOG_PATH"
"""Environment override for the log path, ahead of the ``log_path`` setting.

Public because a *surface* is the thing that needs it: the module reads it when the
registry starts it, so a ``--path`` flag has to be in the environment before the
runtime comes up, and a flag must not be written into the user's settings file."""

FROM_START_ENV = "POEDEX_GAMELOG_FROM_START"
"""Environment flag: read an existing log from byte 0 rather than seeking to EOF.
**Debug only.** The log does not rotate and can reach gigabytes; the product
behaviour is to start at the end, always."""


class ZoneKind(StrEnum):
    """What sort of area was entered.

    Classified from the **area id** (``Generating level 68 area "MapWorldsGrotto"``)
    whenever one is available, because the id is language-independent while the
    display name is translated and, for hideouts, user-themed.
    """

    HIDEOUT = "hideout"
    TOWN = "town"
    MAP = "map"
    OTHER = "other"
    """Anything else: acts, side areas, delve, sanctum, the login screen's own
    areas. Deliberately one bucket — the consumers of Phase 6 only distinguish
    "somewhere the bag is safe to read" from everything else."""


class ZoneSource(StrEnum):
    """Which log line(s) the event was built from. Diagnostic, but load-bearing:
    an event with no ``area_id`` was classified from a translated display name and
    is less trustworthy than one that carries an id."""

    GENERATED = "generated"
    """Only ``Generating level N area "<id>"`` was seen. Carries an id, no name."""

    ENTERED = "entered"
    """Only the ``] : You have entered <name>.`` system line was seen. Carries a
    name, no id — this is what re-entering an already-generated instance looks
    like."""

    PAIRED = "paired"
    """Both, correlated. The good case: language-independent id *and* the name to
    show the user."""


class LogState(StrEnum):
    """What the watcher is doing. Every non-``WATCHING`` state is user-visible."""

    STOPPED = "stopped"
    """Not running."""

    UNAVAILABLE = "unavailable"
    """No candidate path exists. Either Path of Exile is not installed in any
    Steam library we can see, or the library layout is one we do not know. The
    fix is the manual override."""

    WAITING = "waiting"
    """A path is known but there is no file at it yet. Normal before Path of Exile
    has been run once; the watcher keeps checking."""

    WATCHING = "watching"
    """Following a real file. The only healthy state."""


@dataclass(frozen=True, slots=True)
class ZoneEvent:
    """One zone entry, as observed."""

    kind: ZoneKind
    area_id: str | None = None
    """The language-independent id, e.g. ``MapWorldsGrotto``. ``None`` when the
    entry was seen only as a display-name line."""

    name: str | None = None
    """The display name as the client wrote it, e.g. ``The Twilight Strand``.
    Translated and, for hideouts, user-themed. Never a classification key."""

    level: int | None = None
    """Area level from the ``Generating level N`` line, when there was one."""

    source: ZoneSource = ZoneSource.PAIRED
    at: float = 0.0
    """Wall-clock time the line was read, not the timestamp in the log line."""

    log_time: str | None = None
    """The log's own ``YYYY/MM/DD HH:MM:SS`` stamp, verbatim, when parseable."""

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "area_id": self.area_id,
            "name": self.name,
            "level": self.level,
            "source": self.source.value,
            "at": self.at,
            "log_time": self.log_time,
        }


@dataclass(frozen=True, slots=True)
class GameLogStatus:
    """Everything a surface needs to explain the watcher's condition."""

    state: LogState
    path: str | None = None
    """The resolved log path, or ``None`` when nothing resolved."""

    origin: str | None = None
    """How the path was found — ``"override"``, ``"library"``, ``"compatdata"``.
    Worth showing: "we are reading the SD card copy" is the answer to a whole
    class of "why is nothing happening" reports."""

    detail: str | None = None
    """Human-readable reason, present whenever ``state`` is not ``WATCHING``."""

    last_event: ZoneEvent | None = None
    searched: tuple[str, ...] = ()
    """Every path probed on the last resolve attempt, in order. This is what makes
    an unresolved log actionable instead of a shrug."""

    @property
    def healthy(self) -> bool:
        return self.state is LogState.WATCHING

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "path": self.path,
            "origin": self.origin,
            "detail": self.detail,
            "healthy": self.healthy,
            "last_event": self.last_event.to_json() if self.last_event else None,
            "searched": list(self.searched),
        }


@runtime_checkable
class GameLogApi(Protocol):
    """What dependents get from ``ctx.require(GameLogApi)``."""

    async def status(self) -> GameLogStatus:
        """State, resolved path, and the reason when it is not healthy."""
        ...

    async def path(self) -> str | None:
        """The log path currently being followed, or ``None``."""
        ...

    async def last_event(self) -> ZoneEvent | None:
        """The most recent zone entry seen this session, if any."""
        ...

    async def resolve(self) -> GameLogStatus:
        """Re-run path resolution now.

        For the "I just installed the game" / "I just moved it to the SD card"
        case, where waiting out the slow re-resolve timer is the wrong answer.
        """
        ...
