"""Finding ``Client.txt``.

Under Proton the log is at ``<library>/steamapps/common/Path of Exile/logs/Client.txt``
— a **native Linux path, not inside the Wine prefix** (research §4, verified against
Awakened PoE Trade's Linux build). It is still wrong to hardcode it: on a Deck the SD
card is a second Steam library and its mount point has moved across SteamOS releases,
which is exactly the case ``libraryfolders.vdf`` exists to answer.

Resolution order:

1. a manual override, used exclusively when set — if the user says where it is, we do
   not go looking somewhere else and silently follow the wrong file;
2. for each Steam root, for each library that root declares, each known layout in
   :data:`LAYOUTS`, in order.

Nothing here imports a VDF library. :func:`parse_vdf` is ~50 lines and the alternative
is a dependency that would have to be vendored into ``py_modules/`` for the Decky
backend, which has no pip at install time (CLAUDE.md).

**Read-only.** Nothing in this module opens a file for writing.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.log import get_logger

__all__ = [
    "APPID",
    "LAYOUTS",
    "LOG_NAMES",
    "STEAM_ROOTS",
    "LogLocation",
    "candidates",
    "library_roots",
    "locate",
    "parse_vdf",
    "steam_roots",
]

_log = get_logger("module.gamelog.locate")

APPID = "238960"
"""Path of Exile's Steam app id. Used only for the ``compatdata`` fallbacks."""

STEAM_ROOTS: tuple[str, ...] = (
    "~/.steam/steam",
    "~/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
)
"""Where a Steam installation's root may be. The first is the symlink Steam
maintains, the second the real directory it usually points at, the third Flatpak.
Probed in that order and de-duplicated by resolved path, so the common case of the
symlink and its target both existing costs one scan, not two."""

LAYOUTS: tuple[tuple[str, str], ...] = (
    (
        "library",
        "steamapps/common/Path of Exile/logs",
    ),
    (
        "compatdata",
        f"steamapps/compatdata/{APPID}/pfx/drive_c/users/steamuser/Documents/"
        "My Games/Path of Exile/logs",
    ),
    (
        "compatdata",
        f"steamapps/compatdata/{APPID}/pfx/drive_c/Program Files (x86)/"
        "Grinding Gear Games/Path of Exile/logs",
    ),
)
"""Directories that may hold the log, relative to a library root, in probe order.

The first is the verified one and is where the file actually is under Proton. The two
``compatdata`` entries are fallbacks for a prefix that, for whatever reason, redirects
the game's writes inside itself; they are *unverified* — kept because probing a
nonexistent path costs one ``stat`` and being wrong about this costs the whole feature.
"""

LOG_NAMES: tuple[str, ...] = ("Client.txt", "LatestClient.txt")
"""``Client.txt`` is the file. ``LatestClient.txt`` is reported by one source as a
sibling written by some clients; **unconfirmed for PoE 1**, probed second so it can
only ever be a fallback."""


@dataclass(frozen=True, slots=True)
class LogLocation:
    """A path we would follow, and how we arrived at it."""

    path: Path
    origin: str
    """``"override"``, ``"library"`` or ``"compatdata"``."""

    exists: bool
    """Whether there is a file there *right now*. A location with
    ``exists=False`` is still worth returning: the game's directory can be present
    before ``logs/Client.txt`` is, because the log is not created until Path of
    Exile has been run once."""


# -- VDF ------------------------------------------------------------------------
#
# Valve KeyValues: quoted "key" "value" pairs and quoted "key" { ... } blocks,
# `//` comments, backslash escapes. Conditionals ([$WIN32]) and #include are not
# used by libraryfolders.vdf and are skipped rather than interpreted.

_TOKEN = re.compile(
    r"""
    "(?P<quoted>(?:[^"\\]|\\.)*)"     # "quoted string", backslash escapes allowed
    | (?P<brace>[{}])
    | (?P<comment>//[^\n]*)
    | (?P<cond>\[[^\]\n]*\])          # [$WIN32] platform conditional
    | (?P<bare>[^\s{}"]+)             # unquoted token, tolerated
    """,
    re.VERBOSE,
)

_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "r": "\r"}


def _unescape(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            out.append(_ESCAPES.get(nxt, nxt))
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _tokens(text: str) -> Iterator[tuple[str, str]]:
    """``(kind, value)`` pairs where kind is ``"str"``, ``"open"`` or ``"close"``."""
    for match in _TOKEN.finditer(text):
        if match.lastgroup == "comment" or match.lastgroup == "cond":
            continue
        if match.lastgroup == "brace":
            yield ("open" if match.group() == "{" else "close", match.group())
        elif match.lastgroup == "quoted":
            yield "str", _unescape(match.group("quoted"))
        else:
            yield "str", match.group("bare")


def parse_vdf(text: str) -> dict[str, Any]:
    """Parse Valve KeyValues text into nested dicts. Values are always strings.

    Malformed input is tolerated rather than fatal: an unbalanced ``}`` ends the
    current block and a trailing key with no value is dropped. A vdf we cannot read
    must degrade to "this library is invisible", never to a crashed watcher.
    """
    root: dict[str, Any] = {}
    stack: list[dict[str, Any]] = [root]
    pending: str | None = None

    for kind, value in _tokens(text):
        if kind == "open":
            if pending is None:
                continue
            child: dict[str, Any] = {}
            stack[-1][pending] = child
            stack.append(child)
            pending = None
        elif kind == "close":
            pending = None
            if len(stack) > 1:
                stack.pop()
        elif pending is None:
            pending = value
        else:
            stack[-1][pending] = value
            pending = None
    return root


def _library_paths_from_vdf(data: dict[str, Any]) -> list[str]:
    """Pull library paths out of either ``libraryfolders.vdf`` dialect.

    Modern (2021+): ``"libraryfolders" { "0" { "path" "/x" ... } ... }``.
    Legacy: ``"LibraryFolders" { "1" "/x" "2" "/y" "TimeNextStatsReport" "..." }``,
    where the numeric keys are the libraries and everything else is noise.
    """
    block = data
    for key in list(data):
        if key.lower() == "libraryfolders" and isinstance(data[key], dict):
            block = data[key]
            break

    found: list[str] = []
    for key, value in block.items():
        if not key.isdigit():
            continue
        if isinstance(value, dict):
            path = value.get("path")
            if isinstance(path, str) and path:
                found.append(path)
        elif isinstance(value, str) and value:
            found.append(value)
    return found


# -- roots and libraries ---------------------------------------------------------


def _existing_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:  # pragma: no cover - permission-denied on an odd mount
        return False


def steam_roots(extra: Iterable[str | os.PathLike[str]] = ()) -> list[Path]:
    """Every Steam root that exists, de-duplicated by resolved path.

    ``~/.steam/steam`` is normally a symlink to ``~/.local/share/Steam``; resolving
    before de-duplicating means we scan that installation once.
    """
    seen: set[Path] = set()
    roots: list[Path] = []
    for raw in (*extra, *STEAM_ROOTS):
        path = Path(raw).expanduser()
        if not _existing_dir(path):
            continue
        try:
            key = path.resolve()
        except OSError:  # pragma: no cover
            key = path
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    return roots


def library_roots(steam_root: Path | str) -> list[Path]:
    """Library roots declared by a Steam installation, the root itself first.

    The root is always a library even when the vdf is missing or unreadable, which
    is the whole point of returning it first: a broken vdf degrades to "only the
    default library is visible", not to "no libraries".
    """
    root = Path(steam_root).expanduser()
    found: list[Path] = [root]
    seen = {_key(root)}

    for relative in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
        vdf = root / relative
        try:
            if not vdf.is_file():
                continue
            text = vdf.read_text("utf-8", errors="replace")
        except OSError as exc:
            _log.warning("cannot read %s: %s", vdf, type(exc).__name__)
            continue
        for raw in _library_paths_from_vdf(parse_vdf(text)):
            library = Path(raw).expanduser()
            key = _key(library)
            if key in seen:
                continue
            seen.add(key)
            found.append(library)
        # Both files describe the same set; the first one that parses wins.
        break

    return found


def _key(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover
        return path


# -- candidates ------------------------------------------------------------------


def candidates(
    *,
    roots: Sequence[str | os.PathLike[str]] | None = None,
    override: str | os.PathLike[str] | None = None,
) -> list[LogLocation]:
    """Every path worth probing, in order, with no filesystem test applied.

    ``roots`` replaces :data:`STEAM_ROOTS` outright when given — that is what the
    tests use, and it is the only way to exercise this offline.
    """
    if override:
        path = Path(override).expanduser()
        return [LogLocation(path=path, origin="override", exists=_is_file(path))]

    out: list[LogLocation] = []
    seen: set[Path] = set()
    installs = [Path(r).expanduser() for r in roots] if roots is not None else steam_roots()
    for steam_root in installs:
        for library in library_roots(steam_root):
            for origin, relative in LAYOUTS:
                directory = library / relative
                for name in LOG_NAMES:
                    path = directory / name
                    if path in seen:
                        continue
                    seen.add(path)
                    out.append(
                        LogLocation(path=path, origin=origin, exists=_is_file(path))
                    )
    return out


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:  # pragma: no cover - permission-denied
        return False


def locate(
    *,
    roots: Sequence[str | os.PathLike[str]] | None = None,
    override: str | os.PathLike[str] | None = None,
) -> tuple[LogLocation | None, list[LogLocation]]:
    """Resolve the log. Returns ``(location, probed)``.

    ``location`` is the first candidate that exists. Failing that it is the first
    ``Client.txt`` candidate whose install directory exists — the game is installed
    but has never been run, so the log will appear at that exact path eventually and
    the watcher should wait there rather than report the game missing. ``None``
    means neither, and the caller should degrade visibly (SPEC §4.6).

    ``probed`` is every path considered, so the failure can name them.
    """
    probed = candidates(roots=roots, override=override)
    for candidate in probed:
        if candidate.exists:
            return candidate, probed
    for candidate in probed:
        if candidate.path.name != LOG_NAMES[0]:
            continue
        # `logs/` may not exist before the first run, so accept the game directory
        # one level up as evidence that this is the right place to wait.
        if _existing_dir(candidate.path.parent) or _existing_dir(candidate.path.parent.parent):
            return candidate, probed
    if probed and probed[0].origin == "override":
        # An override is a statement of intent: honour it even when nothing is
        # there yet, so the state is "waiting for that file" and not "not found".
        return probed[0], probed
    return None, probed
