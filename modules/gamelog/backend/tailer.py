"""Following ``Client.txt`` without ever reading it whole.

The file never rotates and GGG's own forum carries a report of a 2 GB one, so the
first rule is **seek to EOF on open and never read from byte 0**. Everything else
here follows from that file being append-only, occasionally deleted by a user, and
absent entirely until Path of Exile has been run once.

Two objects, split by what they need to know:

* :class:`FileFollower` follows *one path*. Synchronous, no timers, no resolution.
  Each :meth:`FileFollower.poll` does one ``stat`` and returns whatever complete
  lines appeared. That is the whole steady-state cost: one ``stat`` per second.
* :class:`LogWatcher` owns the timers and the path. It re-resolves on a slow timer
  while the log is missing, polls on a fast one while it is present, and reports
  state transitions so a surface can say *why* nothing is happening.

**Read-only, structurally.** The only ``open`` call in this module passes ``"rb"``.
Nothing truncates, moves or writes to the game's file.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import IO

from modules.gamelog.backend.api import LogState
from modules.gamelog.backend.locate import LogLocation, locate
from runtime.log import get_logger

__all__ = [
    "MAX_LINE_BYTES",
    "MAX_READ_PER_POLL",
    "FileFollower",
    "LogWatcher",
    "Resolver",
    "default_resolver",
]

_log = get_logger("module.gamelog.tailer")

MAX_READ_PER_POLL = 4 * 1024 * 1024
"""Bytes consumed per poll. A backlog is drained over several polls instead of in
one blocking read — relevant after a suspend/resume, and the reason a truncation
that replaces the file with a *large* one still cannot stall the loop."""

MAX_LINE_BYTES = 1024 * 1024
"""Cap on the unterminated tail we are willing to hold. A line that never ends is
not a line; without this, a corrupt file with no newlines is an OOM."""

POLL_INTERVAL = 1.0
RESOLVE_INTERVAL = 30.0

Resolver = Callable[[], "tuple[LogLocation | None, Sequence[LogLocation]]"]
"""Returns ``(location, probed)``. Injected so the watcher can be tested without a
filesystem that looks like a Steam install."""


def default_resolver(
    *,
    roots: Sequence[str | os.PathLike[str]] | None = None,
    override: str | os.PathLike[str] | None = None,
) -> Resolver:
    def resolve() -> tuple[LogLocation | None, Sequence[LogLocation]]:
        return locate(roots=roots, override=override)

    return resolve


class FileFollower:
    """Follows one append-only file from wherever it is when we arrive."""

    def __init__(
        self,
        path: Path | str,
        *,
        from_start: bool = False,
        max_read_per_poll: int = MAX_READ_PER_POLL,
        max_line_bytes: int = MAX_LINE_BYTES,
    ) -> None:
        self.path = Path(path)
        self.from_start = from_start
        """Debug affordance only. The product always follows from EOF; replaying an
        existing log from 0 is how you exercise this offline."""

        self._max_read = max_read_per_poll
        self._max_line = max_line_bytes
        self._handle: IO[bytes] | None = None
        self._offset = 0
        self._buffer = b""
        self._identity: tuple[int, int] | None = None
        self._vanished = False
        self._discarding = False
        self.bytes_read = 0
        """Bytes actually pulled off disk. Asserted on by the growth-safety test:
        opening an existing log of any size must leave this at zero."""

        self.reopens = 0
        """How many times the file was re-opened at 0 because it shrank or was
        replaced. Surfaced so "my events stopped" has an explanation."""

    # -- state ---------------------------------------------------------------

    @property
    def open(self) -> bool:
        return self._handle is not None

    @property
    def offset(self) -> int:
        return self._offset

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
        self._handle = None
        self._identity = None
        self._buffer = b""
        self._discarding = False

    # -- the loop's one call --------------------------------------------------

    def poll(self) -> list[str]:
        """Return complete lines appended since the last call.

        A partial trailing write is held back until its newline arrives, so a line
        caught mid-write is never parsed. Missing file, truncation and replacement
        are all handled here rather than by the caller, because all three are
        indistinguishable from "nothing happened" at the call site.
        """
        try:
            stat = os.stat(self.path)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ENOTDIR):
                _log.warning("cannot stat %s: %s", self.path, type(exc).__name__)
            if self._handle is not None:
                # It was there and now it is not. Whatever comes back is a new file
                # and its first byte is news, so remember to start from 0.
                _log.info("%s disappeared; will reopen at 0 if it returns", self.path)
                self._vanished = True
            self.close()
            return []

        identity = (stat.st_dev, stat.st_ino)
        if self._handle is None:
            vanished, self._vanished = self._vanished, False
            if vanished:
                self.reopens += 1
            return self._first_open(stat.st_size, identity, force_start=vanished)

        if identity != self._identity:
            # Deleted and recreated, or swapped in from elsewhere. The old handle
            # points at an unlinked inode that will never grow again.
            _log.info("%s was replaced; reopening at 0", self.path)
            self.close()
            self.reopens += 1
            return self._first_open(stat.st_size, identity, force_start=True)

        if stat.st_size < self._offset:
            _log.info(
                "%s shrank (%d < %d); reopening at 0", self.path, stat.st_size, self._offset
            )
            self.close()
            self.reopens += 1
            return self._first_open(stat.st_size, identity, force_start=True)

        return self._read_forward(stat.st_size)

    # -- internals ------------------------------------------------------------

    def _first_open(
        self, size: int, identity: tuple[int, int], *, force_start: bool = False
    ) -> list[str]:
        try:
            handle = open(self.path, "rb")  # noqa: SIM115 - held for the tailer's life
        except OSError as exc:
            _log.warning("cannot open %s: %s", self.path, type(exc).__name__)
            return []
        self._handle = handle
        self._identity = identity
        self._buffer = b""
        if self.from_start or force_start:
            self._offset = 0
            return self._read_forward(size)
        # The whole point: start at the end. Never read the history.
        self._offset = size
        handle.seek(size)
        return []

    def _read_forward(self, size: int) -> list[str]:
        if size <= self._offset or self._handle is None:
            return []
        wanted = min(size - self._offset, self._max_read)
        self._handle.seek(self._offset)
        chunk = self._handle.read(wanted)
        if not chunk:
            return []
        self._offset += len(chunk)
        self.bytes_read += len(chunk)
        return self._split(chunk)

    def _split(self, chunk: bytes) -> list[str]:
        data = self._buffer + chunk
        pieces = data.split(b"\n")
        # Whatever follows the last newline is an incomplete write. Hold it: a line
        # caught mid-write must not be parsed until it is whole.
        self._buffer = pieces.pop()

        if self._discarding:
            # We gave up on an over-long line; everything up to its newline is the
            # rest of it, not a line of its own.
            if not pieces:
                self._buffer = b""
                return []
            pieces.pop(0)
            self._discarding = False

        if len(self._buffer) > self._max_line:
            _log.warning(
                "discarding a %d-byte unterminated line from %s", len(self._buffer), self.path
            )
            self._buffer = b""
            self._discarding = True

        # errors="replace": the log is UTF-8, but a torn multi-byte sequence at a
        # chunk boundary or a corrupt region must not take the watcher down.
        return [piece.decode("utf-8", errors="replace").rstrip("\r") for piece in pieces]


StateCallback = Callable[[LogState, "LogLocation | None", str | None, Sequence[LogLocation]], None]
LinesCallback = Callable[[Sequence[str]], Awaitable[None] | None]
TickCallback = Callable[[], Awaitable[None] | None]


class LogWatcher:
    """Resolution, polling and re-resolution around a :class:`FileFollower`.

    :meth:`tick` is one iteration and does no sleeping, so the whole state machine
    is testable without a clock. :meth:`run` is that in a loop.
    """

    def __init__(
        self,
        resolver: Resolver,
        *,
        on_lines: LinesCallback,
        on_state: StateCallback | None = None,
        on_tick: TickCallback | None = None,
        poll_interval: float = POLL_INTERVAL,
        resolve_interval: float = RESOLVE_INTERVAL,
        from_start: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolver = resolver
        # Public and rebindable: the module constructs the watcher in one place and
        # attaches itself in another, and a test wants to swap a callback out.
        self.on_lines = on_lines
        self.on_state = on_state
        self.on_tick = on_tick
        self.poll_interval = poll_interval
        self.resolve_interval = resolve_interval
        self._from_start = from_start
        self._clock = clock

        self.state = LogState.STOPPED
        self.location: LogLocation | None = None
        self._target: LogLocation | None = None
        self.detail: str | None = None
        self.searched: tuple[LogLocation, ...] = ()
        self._follower: FileFollower | None = None
        self._last_resolve = 0.0
        self._resolved_once = False
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    # -- resolution ------------------------------------------------------------

    async def resolve(self) -> LogState:
        """Re-run resolution now, adopt the result, and report the real state.

        Async, and it polls before returning, because "I just installed the game,
        re-check" has to answer *watching* rather than *waiting* — resolving and
        then reporting the state before the first poll would announce a spurious
        degraded state on every healthy start.
        """
        self._last_resolve = self._clock()
        self._resolved_once = True
        location, probed = self._resolver()
        self.searched = tuple(probed)

        if location is None:
            self._drop_follower()
            self._target = None
            self._set_state(
                LogState.UNAVAILABLE,
                None,
                f"no Client.txt at any of {len(probed)} probed paths"
                if probed
                else "no Steam installation found; run "
                "'poedex config set gamelog.log_path <path>'",
            )
            return self.state

        if self._follower is None or self._follower.path != location.path:
            self._drop_follower()
            self._follower = FileFollower(location.path, from_start=self._from_start)
        self._target = location
        await self._pump()
        return self.state

    async def _pump(self) -> None:
        """One poll of the follower: read, set the state, deliver the lines."""
        follower = self._follower
        if follower is None:
            return
        lines = follower.poll()
        self._poll_state()
        if lines:
            result = self.on_lines(lines)
            if result is not None:
                await result

    def _poll_state(self) -> LogState:
        """Set WATCHING/WAITING from whether the follower has the file open."""
        follower = self._follower
        if follower is not None and follower.open:
            self._set_state(LogState.WATCHING, self._target, None)
        else:
            self._set_state(
                LogState.WAITING,
                self._target,
                "the log does not exist yet; Path of Exile has to run once",
            )
        return self.state

    def _drop_follower(self) -> None:
        if self._follower is not None:
            self._follower.close()
        self._follower = None

    def _set_state(self, state: LogState, location: LogLocation | None, detail: str | None) -> None:
        changed = state is not self.state or (location and location.path) != (
            self.location and self.location.path
        )
        self.state = state
        self.location = location
        self.detail = detail
        if changed and self.on_state is not None:
            self.on_state(state, location, detail, self.searched)

    # -- the loop --------------------------------------------------------------

    async def tick(self) -> None:
        """One iteration: resolve if due, poll if we can, deliver what we read."""
        now = self._clock()
        due = not self._resolved_once or (
            self.state is not LogState.WATCHING
            and now - self._last_resolve >= self.resolve_interval
        )
        if due:
            await self.resolve()
        else:
            await self._pump()

        if self.on_tick is not None:
            result = self.on_tick()
            if result is not None:
                await result

    async def run(self) -> None:
        """Tick forever, ``poll_interval`` apart, until :meth:`stop`."""
        self._stopping.clear()
        try:
            while not self._stopping.is_set():
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A watcher that dies on one bad poll is a watcher that stops
                    # working silently, which is the failure SPEC §4.6 forbids.
                    _log.exception("gamelog watcher tick failed; continuing")
                # Wait on the stop signal rather than sleeping, so shutdown is
                # immediate instead of up to a poll interval late.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), self.poll_interval)
        finally:
            self._drop_follower()
            self._set_state(LogState.STOPPED, self.location, "watcher stopped")

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="gamelog-watcher")
        return self._task

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Unconditionally, not only in run()'s finally: a task cancelled before it
        # ever ran never enters the try block, so its finally never fires and the
        # open handle would survive the module's shutdown.
        self._drop_follower()
        self._set_state(LogState.STOPPED, self.location, "watcher stopped")
