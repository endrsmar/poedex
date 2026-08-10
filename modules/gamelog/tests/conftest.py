"""Fixtures for the `gamelog` tests.

Deliberately self-contained rather than reaching for ``tests/conftest.py``: a
conftest applies to its own subtree only, and a module that owns its tests should
not depend on the repository-level one to stay isolated.

Everything here builds a *synthetic* Steam installation. No test reads the
developer's ``~/.steam``, requires Path of Exile, or needs a Deck.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from modules.gamelog.backend.tailer import FileFollower, LogWatcher

# Real lines, from SPEC §4.6 and research §4.
GENERATING = (
    '{ts} 1795218 d8  [INFO Client 1636] '
    'Generating level {level} area "{area}" with seed 2049423767'
)
ENTERED = "{ts} 1801062 9b0 [INFO Client 1636] : You have entered {name}."
CHAT = "{ts} 1801062 9b0 [INFO Client 1636] {speaker}: {text}"
TS = "2018/05/13 16:10:14"


def generating(area: str, level: int = 68, ts: str = TS) -> str:
    return GENERATING.format(ts=ts, level=level, area=area)


def entered(name: str, ts: str = TS) -> str:
    return ENTERED.format(ts=ts, name=name)


def chat(text: str, speaker: str = "Spoofer", ts: str = TS) -> str:
    return CHAT.format(ts=ts, speaker=speaker, text=text)


@pytest.fixture(autouse=True)
def _no_real_steam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make an accidental fall-through to the real filesystem fail loudly."""
    for leaked in ("POEDEX_GAMELOG_PATH", "POEDEX_GAMELOG_FROM_START"):
        monkeypatch.delenv(leaked, raising=False)


@pytest.fixture
def steam(tmp_path: Path) -> SteamTree:
    return SteamTree(tmp_path / "steam-root")


class SteamTree:
    """A throwaway Steam installation you can add libraries and games to."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (self.root / "steamapps").mkdir(parents=True)
        self.libraries: list[Path] = [self.root]

    # -- construction ----------------------------------------------------------

    def add_library(self, path: Path) -> Path:
        (path / "steamapps").mkdir(parents=True, exist_ok=True)
        self.libraries.append(path)
        return path

    def write_libraryfolders(self, *, where: str = "steamapps", legacy: bool = False) -> Path:
        """Write a ``libraryfolders.vdf`` describing every added library."""
        target = self.root / where / "libraryfolders.vdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _legacy_vdf(self.libraries) if legacy else _modern_vdf(self.libraries),
            encoding="utf-8",
        )
        return target

    def install(
        self,
        library: Path | None = None,
        *,
        layout: str = "common",
        name: str = "Client.txt",
        create_log: bool = True,
    ) -> Path:
        """Create a Path of Exile install under ``library`` and return the log path."""
        directory = (library or self.root) / _LAYOUTS[layout]
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / name
        if create_log:
            log.write_text("", encoding="utf-8")
        return log


_LAYOUTS = {
    "common": "steamapps/common/Path of Exile/logs",
    "mygames": (
        "steamapps/compatdata/238960/pfx/drive_c/users/steamuser/Documents/"
        "My Games/Path of Exile/logs"
    ),
    "ggg": (
        "steamapps/compatdata/238960/pfx/drive_c/Program Files (x86)/"
        "Grinding Gear Games/Path of Exile/logs"
    ),
}


def _modern_vdf(libraries: list[Path]) -> str:
    entries = "\n".join(
        f'''\t"{index}"
\t{{
\t\t"path"\t\t"{_escape(library)}"
\t\t"label"\t\t""
\t\t"contentid"\t\t"123456789"
\t\t"apps"
\t\t{{
\t\t\t"238960"\t\t"32000000000"
\t\t}}
\t}}'''
        for index, library in enumerate(libraries)
    )
    return f'"libraryfolders"\n{{\n{entries}\n}}\n'


def _legacy_vdf(libraries: list[Path]) -> str:
    # The pre-2021 dialect: numbered keys map straight to paths, mixed in with
    # non-numeric bookkeeping keys that must be ignored.
    extra = '\t"TimeNextStatsReport"\t\t"1600000000"\n\t"ContentStatsID"\t\t"-1"\n'
    entries = "".join(
        f'\t"{index + 1}"\t\t"{_escape(library)}"\n' for index, library in enumerate(libraries[1:])
    )
    return f'"LibraryFolders"\n{{\n{extra}{entries}}}\n'


def _escape(path: Path) -> str:
    return str(path).replace("\\", "\\\\")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
def make_follower() -> Iterator[Callable[..., FileFollower]]:
    """Build followers that are guaranteed to be closed at teardown.

    A follower holds an open handle for its lifetime, which is correct — and it
    means a test that drops one on the floor leaks a file descriptor. The suite
    runs with ``filterwarnings = ["error"]``, so that leak is a failure rather
    than a shrug, and this fixture is how it stays one.
    """
    made: list[FileFollower] = []

    def make(path: Path, **kwargs: object) -> FileFollower:
        follower = FileFollower(path, **kwargs)  # type: ignore[arg-type]
        made.append(follower)
        return follower

    yield make
    for follower in made:
        follower.close()


@pytest.fixture
def make_watcher() -> Iterator[Callable[..., LogWatcher]]:
    """Same, for watchers: their follower is closed even if the test fails."""
    made: list[LogWatcher] = []

    def make(*args: object, **kwargs: object) -> LogWatcher:
        watcher = LogWatcher(*args, **kwargs)  # type: ignore[arg-type]
        made.append(watcher)
        return watcher

    yield make
    for watcher in made:
        watcher._drop_follower()


@pytest.fixture
def append(tmp_path: Path) -> Iterator[Callable[[Path, str], None]]:
    """Append text to a file the way the game does: open, write, close."""

    def _append(path: Path, text: str) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)

    yield _append
