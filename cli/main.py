"""`poedex` — the command line.

Commands:

    poedex auth set [--account NAME]   store a POESESSID from a hidden prompt
    poedex auth status                 print credential state, never the value
    poedex auth clear                  delete the stored credential
    poedex modules                     list modules, their state and their reason
    poedex sync                        fetch the bag and print the normalized model
    poedex limits                      print what the rate limiter currently knows
    poedex selftest freshness          the in-game freshness experiment (SPEC §4.3)

**The credential is never accepted as an argument.** ``argv`` is visible to every
process on the machine through ``/proc``, and it lands in shell history. The only
input path is :func:`getpass.getpass`, which reads from the terminal with echo off.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.util
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from cli.selftest import DEFAULT_INTERVAL, DEFAULT_SECONDS, MIN_INTERVAL, cmd_freshness
from cli.sync import cmd_sync, render_limits
from modules.credentials.backend.api import CredentialError, CredentialsApi, CredentialState
from modules.poeapi.backend.api import PoeApi
from runtime.errors import PoedexError
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

    sync = sub.add_parser(
        "sync",
        help="fetch the bag and print the normalized item model",
        description=(
            "Fetches this character's backpack and prints SPEC §4.5's normalized "
            "model. No pricing yet — this is here so the structure can be checked "
            "before anything is built on it."
        ),
    )
    sync.add_argument("--character", help="character name (default: most recently played)")
    sync.add_argument(
        "--equipment", action="store_true", help="also print worn gear, not just the bag"
    )
    sync.add_argument(
        "--force",
        action="store_true",
        help=(
            "ignore the cache TTL. Without it, sync honours poeapi.items_ttl_seconds "
            "(0 by default, so every run fetches; raise it while developing to stop "
            "spending budget on repeat runs)."
        ),
    )

    sub.add_parser("limits", help="print the rate limiter's current view")

    selftest = sub.add_parser("selftest", help="experiments that need a human")
    selftest_sub = selftest.add_subparsers(dest="selftest_command", required=True)
    freshness = selftest_sub.add_parser(
        "freshness",
        help="poll while you play, to confirm when the endpoint commits (SPEC §4.3)",
        description=(
            "Polls the character endpoint and prints a timestamped table of "
            "normalized-hash changes. You have to be in the game: pick an item up "
            "mid-map, then portal to your hideout. Spends real rate-limit budget."
        ),
    )
    freshness.add_argument("--character", help="character name (default: most recently played)")
    freshness.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"seconds between polls (minimum {MIN_INTERVAL:.0f})",
    )
    freshness.add_argument(
        "--seconds", type=float, default=DEFAULT_SECONDS, help="how long to run"
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


async def cmd_limits(registry: Registry) -> int:
    """What the limiter has learned. Useful when `sync` says "not yet"."""
    poeapi = registry.api(PoeApi)
    snapshots = poeapi.limits()
    if not snapshots:
        print("nothing learned yet — no request has been made this session")
        return 0
    print(render_limits(snapshots))
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
    elif args.command == "sync":

        async def runner(registry: Registry) -> int:
            return await cmd_sync(
                registry.api(PoeApi),
                character=args.character,
                refresh=args.force,
                equipment=args.equipment,
            )

    elif args.command == "limits":
        runner = cmd_limits
    elif args.command == "selftest":

        async def runner(registry: Registry) -> int:
            return await cmd_freshness(
                registry.api(PoeApi),
                character=args.character,
                interval=args.interval,
                seconds=args.seconds,
            )

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
