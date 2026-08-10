"""Turning ``Client.txt`` lines into zone events.

Lines look like::

    2018/05/13 16:10:14 1801062 9b0 [INFO Client 1636] : You have entered The Twilight Strand.
    2018/05/13 16:10:08 1795218 d8  [INFO Client 1636] Generating level 83 area "MapWorldsGrotto" …

Three gotchas, all from mapwatch's regression tests (research §4), all load-bearing:

**1. Anchor on ``] : ``.** The log records local chat verbatim, so a player standing
in your instance can type "You have entered Hall of Grandmasters" and, if you match on
the substring, drive your tool. Chat lines are ``] <speaker>: <text>``; system messages
are ``] : <text>``. The space before the colon is the entire difference and it is the
only thing between this module and a remotely-triggerable state machine.

**2. Strip ``<<set:..>>``.** Non-English clients prepend gender/plurality markers to
translated strings.

**3. Classify on the area id.** ``Generating level 83 area "MapWorldsGrotto"`` carries
a language-independent id. The display name in the "entered" line is translated, and
for hideouts it is whatever the user named it. Classifying on the name is how a
Portuguese client with a hideout called "Canal" gets read as a map.

Correlating the two lines is :class:`ZoneTracker`'s job. The generating line comes
first and the entered line confirms it, so the tracker holds a generated area briefly
and pairs it with the entered line that follows. If no entered line follows within
:data:`PAIR_WINDOW` seconds — which is what a non-English client looks like, since its
phrase does not match — the generated area is emitted on its own. Nothing is lost;
the id was always the better key.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from modules.gamelog.backend.api import ZoneEvent, ZoneKind, ZoneSource
from runtime.log import get_logger

__all__ = [
    "ENTERED_PATTERNS",
    "PAIR_WINDOW",
    "LogLine",
    "ZoneTracker",
    "classify",
    "compile_patterns",
    "parse_line",
    "strip_set_tokens",
]

_log = get_logger("module.gamelog.parse")

PAIR_WINDOW = 2.0
"""Seconds a generated area waits for its "you have entered" line before being
emitted alone. Small: the two lines are written back to back, and the consumer's own
zone-entry debounce is 20 s (SPEC §4.4), so two seconds of pairing latency is free."""

# `2018/05/13 16:10:14 1801062 9b0 [INFO Client 1636] <body>`
#
# The thread id column is padded, so the separator is `\s+`, and the body is taken
# raw — deciding whether it is a system message is the caller's job, because that
# decision is the security boundary and it should be visible at the call site.
_LINE = re.compile(
    r"""^
    (?P<log_time>\d{4}/\d{2}/\d{2}\ \d{2}:\d{2}:\d{2})
    \s+\d+                       # milliseconds since client start
    \s+[0-9a-fA-F]+              # thread id, hex
    \s+\[(?P<tag>[^\]]*)\]       # [INFO Client 1636]
    (?P<body>.*)$
    """,
    re.VERBOSE,
)

_SET_TOKEN = re.compile(r"<<set:[^>]*>>")

_GENERATING = re.compile(
    r'Generating\s+level\s+(?P<level>\d+)\s+area\s+"(?P<area_id>[^"]+)"'
)

ENTERED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^You have entered (?P<name>.+?)\.\s*$"),
)
"""Localized forms of the zone-entry system message.

Only the English one ships, and it is the only one verified. Translations are *not*
guessed here: a wrong pattern is worse than a missing one, because the fallback for a
missing one is the ``Generating`` line, which carries the better key anyway. A
non-English user can add their client's phrasing through the module's
``entered_patterns`` setting."""


def strip_set_tokens(text: str) -> str:
    """Remove ``<<set:MS>>``-style gender/plurality markers."""
    return _SET_TOKEN.sub("", text)


def compile_patterns(extra: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    """:data:`ENTERED_PATTERNS` plus user-supplied regexes, bad ones dropped.

    A user's typo in a settings field must not take the watcher down with it, so an
    uncompilable pattern is logged and skipped.
    """
    out = list(ENTERED_PATTERNS)
    for raw in extra:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            out.append(re.compile(raw))
        except re.error as exc:
            _log.warning("ignoring unusable entered_patterns entry: %s", exc)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class LogLine:
    """A structurally valid Client.txt line."""

    log_time: str
    tag: str
    body: str
    """Everything after ``]``, with ``<<set:..>>`` markers removed and the outer
    whitespace trimmed. For a system message this is the message; for a chat line it
    still contains the speaker."""

    is_system: bool
    """True only when the raw line had the ``] : `` system prefix. This is the
    anti-spoofing bit; nothing else in this module may infer it."""


def parse_line(line: str) -> LogLine | None:
    """Parse one line, or ``None`` if it is not a Client.txt log line."""
    match = _LINE.match(line.rstrip("\r\n"))
    if match is None:
        return None
    raw_body = match.group("body")
    # The system prefix, checked before any stripping: `] : message`. A chat line is
    # `] speaker: message`, and no speaker is empty, so this cannot be spoofed from
    # inside the game.
    is_system = raw_body.startswith(" : ")
    body = raw_body[3:] if is_system else raw_body
    return LogLine(
        log_time=match.group("log_time"),
        tag=match.group("tag"),
        body=strip_set_tokens(body).strip(),
        is_system=is_system,
    )


# -- classification ---------------------------------------------------------------
#
# Keyed on the area id, which is language-independent. Every rule here is a
# statement about GGG's naming, so each one says how confident it is.

_HIDEOUT_ID = re.compile(r"hideout", re.IGNORECASE)
"""Hideout ids contain "Hideout" — `HideoutCanals`, `HarvestHideout`, and the guild
variants. Substring rather than prefix because the word's position has varied."""

_MAP_ID = re.compile(r"^(?:map|expedition_map)", re.IGNORECASE)
"""Endgame map ids begin with `Map` (`MapWorldsGrotto`, `MapAtziri1`). Verified by
the example in SPEC §4.6."""

_TOWN_ID = re.compile(r"^\d+_town$", re.IGNORECASE)
"""Act towns are `1_town` … `10_town`."""

_TOWN_EXTRA_IDS = frozenset({"heisthub", "highgate", "templelevel1_1"})
"""Non-`N_town` hubs. `HeistHub` is the Rogue Harbour. **Unverified** — these are
best-effort; being wrong downgrades a town to `other`, which costs one skipped
auto-sync, not correctness."""

_HIDEOUT_NAME = re.compile(r"\bhideout\b", re.IGNORECASE)
"""Last-resort fallback for an entry seen only as a display name (re-entering an
already-generated instance). English-only and user-themed names defeat it, which is
precisely why it is the fallback and not the rule."""


def classify(area_id: str | None, name: str | None = None) -> ZoneKind:
    """Classify an area, preferring the id and falling back to the display name."""
    if area_id:
        if _HIDEOUT_ID.search(area_id):
            return ZoneKind.HIDEOUT
        if _TOWN_ID.match(area_id) or area_id.lower() in _TOWN_EXTRA_IDS:
            return ZoneKind.TOWN
        if _MAP_ID.match(area_id):
            return ZoneKind.MAP
        return ZoneKind.OTHER
    if name and _HIDEOUT_NAME.search(name):
        return ZoneKind.HIDEOUT
    return ZoneKind.OTHER


# -- correlation ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Generated:
    area_id: str
    level: int
    log_time: str
    seen_at: float

    def as_event(self, now: float) -> ZoneEvent:
        return ZoneEvent(
            kind=classify(self.area_id),
            area_id=self.area_id,
            name=None,
            level=self.level,
            source=ZoneSource.GENERATED,
            at=now,
            log_time=self.log_time,
        )


class ZoneTracker:
    """Stateful line-to-event translation.

    Separate from the tailer so it can be driven by a list of strings in a test, and
    separate from the module so the event bus is not involved in parsing.
    """

    def __init__(
        self,
        *,
        entered_patterns: Iterable[re.Pattern[str]] | None = None,
        pair_window: float = PAIR_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._patterns = tuple(entered_patterns) if entered_patterns else ENTERED_PATTERNS
        self._pair_window = pair_window
        self._clock = clock
        self._pending: _Generated | None = None

    @property
    def pending_area_id(self) -> str | None:
        """The generated area still waiting for its entered line. Diagnostic."""
        return self._pending.area_id if self._pending else None

    def feed(self, line: str) -> list[ZoneEvent]:
        """Consume one line. Returns the events it completed, usually none or one."""
        parsed = parse_line(line)
        if parsed is None:
            return []
        now = self._clock()

        generating = _GENERATING.search(parsed.body)
        if generating is not None:
            # `Generating` is not a system message — it has no `] : ` prefix — so it
            # is *not* protected by the anti-spoofing anchor. It is safe anyway
            # because its shape (`Generating level N area "id" with seed N`) cannot
            # be produced by chat: a chat line always carries `speaker:` before the
            # body, which this pattern would have to appear *after*. Require that.
            if not _speaker_prefix(parsed):
                events = self._expire(now)
                events.extend(self._replace_pending(generating, parsed, now))
                return events
            return self._expire(now)

        if parsed.is_system:
            name = self._match_entered(parsed.body)
            if name is not None:
                return [self._entered(name, parsed, now)]

        return self._expire(now)

    def flush(self, now: float | None = None) -> list[ZoneEvent]:
        """Emit a generated area whose entered line never arrived.

        Called by the tailer once per poll, which is what makes the non-English path
        arrive within a couple of seconds rather than at the next zone change.
        """
        return self._expire(self._clock() if now is None else now)

    def drain(self, now: float | None = None) -> list[ZoneEvent]:
        """Emit any pending area immediately, ignoring the window. For shutdown."""
        if self._pending is None:
            return []
        event = self._pending.as_event(self._clock() if now is None else now)
        self._pending = None
        return [event]

    # -- internals ----------------------------------------------------------------

    def _replace_pending(
        self, generating: re.Match[str], parsed: LogLine, now: float
    ) -> list[ZoneEvent]:
        events: list[ZoneEvent] = []
        if self._pending is not None:
            # Two generations with no entered line between them: the first one
            # happened, we just never saw it confirmed.
            events.append(self._pending.as_event(now))
        self._pending = _Generated(
            area_id=generating.group("area_id"),
            level=int(generating.group("level")),
            log_time=parsed.log_time,
            seen_at=now,
        )
        return events

    def _entered(self, name: str, parsed: LogLine, now: float) -> ZoneEvent:
        pending, self._pending = self._pending, None
        if pending is not None:
            return ZoneEvent(
                kind=classify(pending.area_id, name),
                area_id=pending.area_id,
                name=name,
                level=pending.level,
                source=ZoneSource.PAIRED,
                at=now,
                log_time=parsed.log_time,
            )
        return ZoneEvent(
            kind=classify(None, name),
            area_id=None,
            name=name,
            level=None,
            source=ZoneSource.ENTERED,
            at=now,
            log_time=parsed.log_time,
        )

    def _expire(self, now: float) -> list[ZoneEvent]:
        if self._pending is None or now - self._pending.seen_at < self._pair_window:
            return []
        event = self._pending.as_event(now)
        self._pending = None
        return [event]

    def _match_entered(self, body: str) -> str | None:
        for pattern in self._patterns:
            match = pattern.match(body)
            if match is None:
                continue
            # A user-supplied pattern may use a bare group instead of `(?P<name>…)`.
            name = match.groupdict().get("name")
            if name is None and match.groups():
                name = match.group(1)
            if name:
                return name.strip()
        return None


_SPEAKER = re.compile(r"^\s*[^:]{1,64}:\s")


def _speaker_prefix(parsed: LogLine) -> bool:
    """Whether the body looks like ``<speaker>: text`` — i.e. player chat.

    Used to keep the un-anchored ``Generating`` pattern from matching a chat line
    that quotes it. A system message never has one, by definition: its speaker slot
    is empty, which is what ``] : `` means.
    """
    if parsed.is_system:
        return False
    return bool(_SPEAKER.match(parsed.body))
