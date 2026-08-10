"""`poedex` — the command line.

Commands:

    poedex auth set [--account NAME]   store a POESESSID from a hidden prompt
    poedex auth status                 print credential state, never the value
    poedex auth clear                  delete the stored credential
    poedex modules                     list modules, their state and their reason
    poedex gamelog status              where Client.txt was found, or why not
    poedex gamelog watch               tail it and print classified zone events

**The credential is never accepted as an argument.** ``argv`` is visible to every
process on the machine through ``/proc``, and it lands in shell history. The only
input path is :func:`getpass.getpass`, which reads from the terminal with echo off.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.util
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from modules.credentials.backend.api import CredentialError, CredentialsApi, CredentialState
from modules.gamelog.backend.api import (
    FROM_START_ENV,
    GAMELOG_AVAILABLE,
    GAMELOG_UNAVAILABLE,
    PATH_ENV,
    ZONE_ENTERED,
    GameLogApi,
    GameLogStatus,
)
from runtime.errors import PoedexError
from runtime.events import Event
from runtime.log import install_redaction, silence_noisy_loggers
from runtime.registry import Registry, discover
from runtime.secrets import redact

Command = Callable[[Registry], Awaitable[int]]

PROMPT = "POESESSID (input is hidden, nothing is echoed): "

_STATE_TEXT = {
    CredentialState.NEVER_SET: "not set — run 'poedex auth set'",
    CredentialState.SET: "stored, not yet used against the API",
    CredentialState.OK: "accepted by the API",
    CredentialState.REJECTED: "rejected by the API — it has expired or was revoked",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="poedex", description="PoEDex — Path of Exile assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="manage the POESESSID credential")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    auth_set = auth_sub.add_parser(
        "set",
        help="store a POESESSID, read from a hidden prompt",
        description=(
            "Reads the credential from a hidden prompt. It cannot be passed as an "
            "argument: argv is world-readable via /proc and lands in shell history."
        ),
    )
    auth_set.add_argument("--account", help="account name this credential belongs to")

    auth_sub.add_parser("status", help="print credential state (never the value)")
    auth_sub.add_parser("clear", help="delete the stored credential")

    sub.add_parser("modules", help="list modules and their state")

    gamelog = sub.add_parser("gamelog", help="the read-only Client.txt tail")
    gamelog_sub = gamelog.add_subparsers(dest="gamelog_command", required=True)

    gamelog_status = gamelog_sub.add_parser(
        "status", help="print where Client.txt was found, or every path tried"
    )
    gamelog_watch = gamelog_sub.add_parser(
        "watch",
        help="follow Client.txt and print zone events until interrupted",
        description=(
            "Tails the log from its end and prints each zone entry with the "
            "classification the rest of the tool acts on. Read-only: the game's "
            "file is opened 'rb' and never written to."
        ),
    )
    for command in (gamelog_status, gamelog_watch):
        command.add_argument(
            "--path", help="use this Client.txt instead of probing Steam's libraries"
        )
    gamelog_watch.add_argument(
        "--seconds", type=float, default=None, help="stop after this long (default: forever)"
    )
    gamelog_watch.add_argument(
        "--from-start",
        action="store_true",
        help=(
            "debug only: read the existing file from byte 0. The product never does "
            "this — the log does not rotate and can reach gigabytes."
        ),
    )
    return parser


def modules_root() -> Path:
    """Where the `modules` package lives, in a source tree or an installed wheel."""
    spec = importlib.util.find_spec("modules")
    if spec is not None and spec.submodule_search_locations:
        return Path(next(iter(spec.submodule_search_locations)))
    return Path(__file__).resolve().parent.parent / "modules"  # pragma: no cover


async def _with_runtime(fn: Command) -> int:
    registry = Registry()
    registry.register_all(discover(modules_root()))
    await registry.start_all()
    try:
        return await fn(registry)
    finally:
        await registry.stop_all()


async def cmd_auth_set(registry: Registry, account: str | None) -> int:
    credentials = registry.api(CredentialsApi)
    try:
        value = getpass.getpass(PROMPT)
    except (EOFError, KeyboardInterrupt):
        print("\naborted", file=sys.stderr)
        return 1
    if not value.strip():
        print("nothing entered; credential unchanged", file=sys.stderr)
        return 1
    try:
        status = await credentials.set(value, account)
    except CredentialError as exc:
        # CredentialError messages are built from metadata only, never the value.
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        del value
    print(f"stored. state: {status.state.value}")
    return 0


async def cmd_auth_status(registry: Registry) -> int:
    credentials = registry.api(CredentialsApi)
    status = await credentials.status()
    print(f"state:      {status.state.value} — {_STATE_TEXT[status.state]}")
    print(f"account:    {status.account or '-'}")
    print(f"added:      {status.added_at.isoformat() if status.added_at else '-'}")
    print(f"last ok:    {status.last_ok_at.isoformat() if status.last_ok_at else '-'}")
    if status.rejected_at:
        print(f"rejected:   {status.rejected_at.isoformat()}")
    if status.note:
        print(f"note:       {redact(status.note)}")
    if status.stale:
        print("warning:    not confirmed recently; it may have expired")
    return 0 if status.usable else 1


async def cmd_auth_clear(registry: Registry) -> int:
    credentials = registry.api(CredentialsApi)
    status = await credentials.clear()
    print(f"cleared. state: {status.state.value}")
    return 0


def _print_gamelog_status(status: GameLogStatus) -> None:
    print(f"state:      {status.state.value}")
    print(f"path:       {status.path or '-'}")
    print(f"found via:  {status.origin or '-'}")
    if status.detail:
        print(f"detail:     {status.detail}")
    if status.last_event:
        event = status.last_event
        print(f"last zone:  {event.kind.value} {event.area_id or ''} {event.name or ''}".rstrip())
    if not status.healthy:
        if status.searched:
            print("probed:")
            for candidate in status.searched:
                print(f"  {candidate}")
        print("set a path with: poedex gamelog watch --path /path/to/Client.txt")


async def cmd_gamelog_status(registry: Registry) -> int:
    gamelog = registry.api(GameLogApi)
    # The watcher's first tick is scheduled, not awaited, by start(). Resolve
    # explicitly so a one-shot command reports the real answer rather than
    # whatever the background task happened to reach.
    status = await gamelog.resolve()
    _print_gamelog_status(status)
    return 0 if status.healthy else 1


async def cmd_gamelog_watch(registry: Registry, seconds: float | None) -> int:
    gamelog = registry.api(GameLogApi)

    def show(event: Event) -> None:
        if event.topic == ZONE_ENTERED:
            payload = event.payload
            name = payload.get("name") or payload.get("area_id") or "?"
            level = f" (level {payload['level']})" if payload.get("level") else ""
            print(f"{payload['kind']:<8} {name}{level}  [{payload['source']}]")
        elif event.topic == GAMELOG_UNAVAILABLE:
            print(f"unavailable: {event.payload.get('detail')}", file=sys.stderr)
        elif event.topic == GAMELOG_AVAILABLE:
            print(f"available:   {event.payload.get('path')}")

    # Subscribe before resolving: with --from-start the first resolve already
    # produces events, and a printer attached afterwards would miss them.
    unsubscribe = registry.events.subscribe("gamelog.*", show)
    try:
        _print_gamelog_status(await gamelog.resolve())
        print("watching; Ctrl-C to stop" if seconds is None else f"watching for {seconds}s")
        if seconds is None:  # pragma: no cover - interactive
            await asyncio.Event().wait()
        else:
            await asyncio.sleep(seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover - interactive
        pass
    finally:
        unsubscribe()
    return 0


async def cmd_modules(registry: Registry) -> int:
    for info in registry.status().values():
        line = f"{info['id']:<14} {info['kind']:<8} {info['state']:<10}"
        if info["requires"]:
            line += f" requires={','.join(info['requires'])}"
        if info["reason"]:
            line += f"  ({info['reason']})"
        print(line)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    install_redaction()
    silence_noisy_loggers()
    args = build_parser().parse_args(argv)

    runner: Command
    if args.command == "auth":
        if args.auth_command == "set":
            async def runner(registry: Registry) -> int:
                return await cmd_auth_set(registry, args.account)
        elif args.auth_command == "status":
            runner = cmd_auth_status
        else:
            runner = cmd_auth_clear
    elif args.command == "gamelog":
        # The module reads these when the registry starts it, which is inside
        # _with_runtime — so they have to be in the environment before that, and a
        # flag must not persist into the user's settings file.
        if args.path:
            os.environ[PATH_ENV] = args.path
        if getattr(args, "from_start", False):
            os.environ[FROM_START_ENV] = "1"
        if args.gamelog_command == "status":
            runner = cmd_gamelog_status
        else:
            async def runner(registry: Registry) -> int:
                return await cmd_gamelog_watch(registry, args.seconds)
    else:
        runner = cmd_modules

    try:
        return asyncio.run(_with_runtime(runner))
    except PoedexError as exc:
        print(f"error: {redact(str(exc))}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
