"""Following a file that never rotates, sometimes vanishes, and can be enormous."""

from __future__ import annotations

import ast
import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from modules.gamelog.backend.api import LogState
from modules.gamelog.backend.locate import LogLocation, locate

from .conftest import FakeClock, SteamTree, entered, generating

Append = Callable[[Path, str], None]


@pytest.fixture
def log(tmp_path: Path) -> Path:
    path = tmp_path / "Client.txt"
    path.write_text("old line one\nold line two\n", encoding="utf-8")
    return path


# -- seek to EOF, never read from 0 -------------------------------------------------


def test_the_first_poll_returns_nothing_and_reads_nothing(log: Path, make_follower):
    follower = make_follower(log)
    assert follower.poll() == []
    assert follower.bytes_read == 0, "the history must never be read"
    assert follower.offset == log.stat().st_size


def test_appended_lines_are_returned(log: Path, append: Append, make_follower):
    follower = make_follower(log)
    follower.poll()
    append(log, "new one\nnew two\n")
    assert follower.poll() == ["new one", "new two"]
    assert follower.poll() == []


def test_a_huge_existing_log_is_not_read(tmp_path: Path, append: Append, make_follower):
    """GGG's forum has a report of a 2 GB Client.txt. Reading it is not an option.

    Five megabytes of sparse file stands in for the two gigabytes; what is asserted
    is that *zero* bytes are read, which does not get truer at a larger size.
    """
    log = tmp_path / "Client.txt"
    with open(log, "wb") as handle:
        handle.truncate(5 * 1024 * 1024)
        handle.seek(5 * 1024 * 1024)
        handle.write(b"\n")
    size = log.stat().st_size

    follower = make_follower(log)
    assert follower.poll() == []
    assert follower.bytes_read == 0
    assert follower.offset == size

    append(log, "one appended line\n")
    assert follower.poll() == ["one appended line"]
    assert follower.bytes_read == len(b"one appended line\n")


def test_a_backlog_is_drained_over_several_polls(log: Path, append: Append, make_follower):
    """A large append must not be one blocking read — relevant after a resume."""
    follower = make_follower(log, max_read_per_poll=32)
    follower.poll()
    append(log, "".join(f"line {i}\n" for i in range(20)))
    first = follower.poll()
    assert 0 < len(first) < 20
    lines = list(first)
    while chunk := follower.poll():
        lines.extend(chunk)
    assert lines == [f"line {i}" for i in range(20)]


def test_from_start_is_available_for_debugging(log: Path, make_follower):
    follower = make_follower(log, from_start=True)
    assert follower.poll() == ["old line one", "old line two"]


# -- partial writes -----------------------------------------------------------------


def test_a_partial_trailing_line_is_held_back(log: Path, append: Append, make_follower):
    follower = make_follower(log)
    follower.poll()
    append(log, "2018/05/13 16:10:14 1801062 9b0 [INFO Client 163")
    assert follower.poll() == [], "a line caught mid-write must not be parsed"
    append(log, "6] : You have entered Grotto.\n")
    assert follower.poll() == [
        "2018/05/13 16:10:14 1801062 9b0 [INFO Client 1636] : You have entered Grotto."
    ]


def test_a_line_split_across_polls_byte_by_byte_reassembles(
    log: Path, append: Append, make_follower
):
    follower = make_follower(log)
    follower.poll()
    for char in "hello\n":
        append(log, char)
        result = follower.poll()
    assert result == ["hello"]


def test_an_unterminated_line_cannot_grow_without_bound(log: Path, append: Append, make_follower):
    follower = make_follower(log, max_line_bytes=64)
    follower.poll()
    append(log, "x" * 500)
    assert follower.poll() == []
    append(log, "\nrecovered\n")
    # The junk is dropped; the next real line still arrives.
    assert follower.poll() == ["recovered"]


def test_undecodable_bytes_do_not_crash_the_follower(log: Path, make_follower):
    follower = make_follower(log)
    follower.poll()
    with open(log, "ab") as handle:
        handle.write(b"caf\xff\xfe bytes\n")
    lines = follower.poll()
    assert len(lines) == 1
    assert lines[0].startswith("caf")


# -- truncation, replacement, absence -------------------------------------------------


def test_truncation_reopens_at_zero(log: Path, append: Append, make_follower):
    follower = make_follower(log)
    follower.poll()
    with open(log, "w", encoding="utf-8") as handle:
        handle.write("after the truncation\n")
    assert follower.poll() == ["after the truncation"]
    assert follower.reopens == 1


def test_a_replaced_file_is_reopened_even_at_the_same_size(log: Path, make_follower):
    """Same size, different inode: `size < offset` alone would miss this."""
    follower = make_follower(log)
    follower.poll()
    replacement = log.with_name("replacement")
    replacement.write_text("brand new content!\n", encoding="utf-8")
    os.replace(replacement, log)
    assert follower.poll() == ["brand new content!"]
    assert follower.reopens == 1


def test_a_missing_file_is_not_an_error(tmp_path: Path, make_follower):
    follower = make_follower(tmp_path / "never-existed" / "Client.txt")
    assert follower.poll() == []
    assert not follower.open


def test_absence_then_appearance(tmp_path: Path, append: Append, make_follower):
    """The log does not exist until Path of Exile has been run once."""
    log = tmp_path / "Client.txt"
    follower = make_follower(log)
    assert follower.poll() == []

    log.write_text("history that predates us\n", encoding="utf-8")
    assert follower.poll() == [], "arriving late is still arriving at EOF"
    assert follower.open

    append(log, "live line\n")
    assert follower.poll() == ["live line"]


def test_disappearance_then_reappearance(log: Path, append: Append, make_follower):
    follower = make_follower(log)
    follower.poll()
    log.unlink()
    assert follower.poll() == []
    assert not follower.open

    log.write_text("recreated\nsecond\n", encoding="utf-8")
    # Recreated from nothing: read it whole, because "from 0" *is* its end-state.
    assert follower.poll() == ["recreated", "second"]


def test_the_game_log_is_never_written_to(log: Path, append: Append, make_follower):
    before = log.read_bytes()
    follower = make_follower(log)
    follower.poll()
    append(log, "a line\n")
    follower.poll()
    follower.close()
    assert log.read_bytes() == before + b"a line\n", "we appended; the follower did not"


def test_no_backend_file_can_open_the_log_for_writing():
    """Structural, not behavioural: every `open()` in the backend is read-only.

    The behavioural test above proves this run did not write. This proves no code
    path *could*, which is the promise the project makes about the game's files.
    """
    backend = Path(__file__).resolve().parent.parent / "backend"
    calls = 0
    for source in sorted(backend.glob("*.py")):
        tree = ast.parse(source.read_text("utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name not in {"open", "write_text", "write_bytes", "truncate", "unlink"}:
                continue
            assert name == "open", f"{source.name}:{node.lineno} calls {name}()"
            modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
            modes += [
                k.value.value
                for k in node.keywords
                if k.arg == "mode" and isinstance(k.value, ast.Constant)
            ]
            assert modes, f"{source.name}:{node.lineno} opens with the default mode"
            for mode in modes:
                assert set(mode) <= set("rb"), f"{source.name}:{node.lineno} opens {mode!r}"
            calls += 1
    assert calls == 1, f"expected exactly one open() in the backend; found {calls}"


# -- the watcher ----------------------------------------------------------------------


def _resolver(location: LogLocation | None, probed=()):
    def resolve():
        return location, probed

    return resolve


class Sink:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.states: list[LogState] = []

    def on_lines(self, lines) -> None:
        self.lines.extend(lines)

    def on_state(self, state, location, detail, searched) -> None:
        self.states.append(state)


async def test_the_watcher_reports_watching_and_delivers_lines(
    log: Path, append: Append, make_watcher
):
    sink = Sink()
    watcher = make_watcher(
        _resolver(LogLocation(path=log, origin="library", exists=True)),
        on_lines=sink.on_lines,
        on_state=sink.on_state,
    )
    await watcher.tick()
    assert watcher.state is LogState.WATCHING
    append(log, generating("HideoutCanals") + "\n")
    await watcher.tick()
    assert len(sink.lines) == 1
    assert sink.states == [LogState.WATCHING]


async def test_an_unresolvable_log_is_reported_not_swallowed(make_watcher):
    """SPEC §4.6: never degrade silently."""
    sink = Sink()
    watcher = make_watcher(
        _resolver(None, probed=[LogLocation(Path("/nope/Client.txt"), "library", False)]),
        on_lines=sink.on_lines,
        on_state=sink.on_state,
    )
    await watcher.tick()
    assert watcher.state is LogState.UNAVAILABLE
    assert sink.states == [LogState.UNAVAILABLE]
    assert watcher.detail and "probed" in watcher.detail


async def test_a_known_path_with_no_file_is_waiting_not_unavailable(tmp_path: Path, make_watcher):
    sink = Sink()
    log = tmp_path / "Client.txt"
    watcher = make_watcher(
        _resolver(LogLocation(path=log, origin="override", exists=False)),
        on_lines=sink.on_lines,
        on_state=sink.on_state,
    )
    await watcher.tick()
    assert watcher.state is LogState.WAITING

    log.write_text("", encoding="utf-8")
    await watcher.tick()
    assert watcher.state is LogState.WATCHING
    assert sink.states == [LogState.WAITING, LogState.WATCHING]


async def test_resolution_is_retried_only_on_the_slow_timer(tmp_path: Path, make_watcher):
    clock = FakeClock()
    calls: list[float] = []

    def resolve():
        calls.append(clock.now)
        return None, ()

    watcher = make_watcher(
        resolve, on_lines=lambda lines: None, resolve_interval=30.0, clock=clock
    )
    await watcher.tick()
    for _ in range(5):
        clock.advance(1.0)
        await watcher.tick()
    assert len(calls) == 1, "a missing log must not be re-probed every second"

    clock.advance(30.0)
    await watcher.tick()
    assert len(calls) == 2


async def test_a_watching_log_is_not_re_probed_at_all(log: Path, make_watcher):
    clock = FakeClock()
    calls: list[float] = []

    def resolve():
        calls.append(clock.now)
        return LogLocation(path=log, origin="library", exists=True), ()

    watcher = make_watcher(resolve, on_lines=lambda lines: None, clock=clock)
    await watcher.tick()
    for _ in range(100):
        clock.advance(1.0)
        await watcher.tick()
    assert len(calls) == 1


async def test_the_watcher_survives_a_failing_callback(log: Path, append: Append, make_watcher):
    """A watcher that dies on one bad line stops working silently. It must not."""
    seen: list[str] = []

    def explode(lines):
        seen.extend(lines)
        raise RuntimeError("consumer bug")

    watcher = make_watcher(
        _resolver(LogLocation(path=log, origin="library", exists=True)),
        on_lines=explode,
        poll_interval=0.01,
    )
    await watcher.tick()
    append(log, "one\n")
    with pytest.raises(RuntimeError):
        await watcher.tick()  # tick() itself propagates
    task = watcher.start()  # run() does not
    append(log, "two\n")
    for _ in range(100):
        if len(seen) >= 2:
            break
        await _sleep_a_tick()
    await watcher.stop()
    assert seen == ["one", "two"], "the loop kept going after the handler raised"
    assert task.done()


async def test_stop_is_idempotent_and_closes_the_file(log: Path, make_watcher):
    watcher = make_watcher(
        _resolver(LogLocation(path=log, origin="library", exists=True)),
        on_lines=lambda lines: None,
        poll_interval=0.01,
    )
    watcher.start()
    await _sleep_a_tick()
    await watcher.stop()
    await watcher.stop()
    assert watcher.state is LogState.STOPPED


async def test_the_watcher_runs_end_to_end_against_a_synthetic_steam_tree(
    steam: SteamTree, append: Append
, make_watcher):
    """The one test that exercises locate + follow together."""
    log = steam.install()
    sink = Sink()
    watcher = make_watcher(
        lambda: locate(roots=[steam.root]), on_lines=sink.on_lines, on_state=sink.on_state
    )
    await watcher.tick()
    assert watcher.state is LogState.WATCHING
    append(log, generating("HideoutCanals") + "\n" + entered("Canal Hideout") + "\n")
    await watcher.tick()
    assert len(sink.lines) == 2


async def _sleep_a_tick() -> None:
    await asyncio.sleep(0.02)
