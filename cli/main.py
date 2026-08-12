"""`poedex` — the command line.

Commands:

    poedex auth set [--account NAME]   store a POESESSID from a hidden prompt
    poedex auth status                 print credential state, never the value
    poedex auth clear                  delete the stored credential
    poedex config list                 every setting, its value, and where it came from
    poedex config get|set|unset KEY    read or write one <module>.<key>
    poedex modules                     list modules, their state and their reason
    poedex gamelog status              where Client.txt was found, or why not
    poedex gamelog watch               tail it and print classified zone events
    poedex sync                        fetch the bag and print the normalized model
    poedex value                       price the bag: per-item values and a total
    poedex appraise                    the bag, judged: keep/check/trash/unpriceable
    poedex stash                       the tab list: freshness, contents, value
    poedex stash tab N                 one tab, judged at stash strictness
    poedex stash plan                  what a full refresh would cost right now
    poedex stash crawl --yes           the cold crawl. Minutes. Never automatic
    poedex price UID [--mods ...]      ask the market about one item, your query
    poedex limits                      print what the rate limiter currently knows
    poedex moddb                       the mod database: how old, and what it says
    poedex serve                       the web surface on http://127.0.0.1:7331
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
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from cli.appraise import cmd_appraise
from cli.characters import cmd_characters
from cli.config import cmd_config
from cli.moddb import cmd_moddb
from cli.price import cmd_price
from cli.selftest import DEFAULT_INTERVAL, DEFAULT_SECONDS, MIN_INTERVAL, cmd_freshness
from cli.serve import DEFAULT_PORT, cmd_serve
from cli.stash import cmd_stash_crawl, cmd_stash_list, cmd_stash_plan, cmd_stash_tab
from cli.sync import cmd_sync, render_limits
from cli.value import cmd_value
from modules.appraisal.backend.api import AppraisalApi, Strictness
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
from modules.moddb.backend.api import Origin as ModOrigin
from modules.poeapi.backend.api import CHARACTER_ENV, PoeApi
from modules.prices.backend.api import PricesApi
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


def _add_league_argument(command: argparse.ArgumentParser) -> None:
    """``--league``, on every command that turns items into money.

    An override, not a selector. The default is the league of the character whose
    bag is being read, which is the only source that cannot disagree with reality;
    this flag is for deliberately asking "what would this be worth in Standard?",
    and the output labels the answer as an override when it is used.
    """
    command.add_argument(
        "--league",
        default=None,
        help=(
            "price against this league instead of the character's own. Overrides "
            "the prices.league setting for this run; the output says so."
        ),
    )


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
    auth_set.add_argument(
        "--account",
        help=(
            "account name this credential belongs to. Optional — it is read off "
            "your profile on the first request that needs it. Pass it only to "
            "override a derived name that is wrong"
        ),
    )

    auth_sub.add_parser("status", help="print credential state (never the value)")
    auth_sub.add_parser("clear", help="delete the stored credential")

    config = sub.add_parser(
        "config",
        help="read and write settings",
        description=(
            "Settings live in ~/.config/poedex/settings.json, owner-readable only, "
            "keyed by module. Every setting a started module registers is listed "
            "here with its schema, so a value written through 'set' is validated "
            "against the same rules the module itself sees. The POESESSID is not a "
            "setting and cannot be read or written here."
        ),
    )
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_list = config_sub.add_parser(
        "list", help="every registered setting, its value, and whether it is stored"
    )
    config_list.add_argument(
        "--verbose", action="store_true", help="also print each setting's description"
    )
    config_get = config_sub.add_parser("get", help="print one setting in full")
    config_get.add_argument("key", metavar="MODULE.KEY", help="for example net.contact")
    config_set = config_sub.add_parser(
        "set", help="store a value, validated against the registered schema"
    )
    config_set.add_argument("key", metavar="MODULE.KEY")
    config_set.add_argument("value", help="a list or a dict is written as JSON")
    config_unset = config_sub.add_parser("unset", help="drop the stored value, back to the default")
    config_unset.add_argument("key", metavar="MODULE.KEY")

    characters = sub.add_parser(
        "characters",
        help="the account's characters, and which one the tool would read",
        description=(
            "Lists every character with its league, class, level and last-played "
            "time, and then says which one the tool would read *and why*. The "
            "league column is the one that matters: it is what tells a parked "
            "character from the one you play, and reading the wrong one is a "
            "silent four-fold error on every price. Costs one cached request."
        ),
    )
    characters.add_argument(
        "--force",
        action="store_true",
        help=(
            "re-fetch the character list. Rarely useful: get-characters is the "
            "tightest endpoint on the account and has a minimum interval this "
            "cannot shorten."
        ),
    )

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

    value = sub.add_parser(
        "value",
        help="price the bag and print per-item values and a total",
        description=(
            "Prices the backpack from poe.ninja's bulk tables and the player's own "
            "~price notes. No verdicts and no thresholds — those are Phase 4. "
            "Pricing costs zero GGG rate-limit budget; only the inventory fetch does."
        ),
    )
    value.add_argument("--character", help="character name (default: most recently played)")
    value.add_argument("--force", action="store_true", help="ignore the inventory cache TTL")
    value.add_argument(
        "--refresh-prices",
        action="store_true",
        help=(
            "re-fetch every price table, ignoring both the 30-minute TTL and the "
            "stored ETag. Normally unnecessary: the tables are prefetched at start."
        ),
    )
    _add_league_argument(value)

    appraise = sub.add_parser(
        "appraise",
        help="the bag, judged: keep / check / trash / unpriceable, with a total",
        description=(
            "Prices the backpack and turns each price into a verdict. Four states, "
            "and 'unpriceable' is one of them: an item the price index does not "
            "carry is excluded from the total and counted separately, never folded "
            "into 'trash' and never summed as zero. Costs exactly one account "
            "request and no trade requests at all: a rare is *highlighted*, never "
            "auto-priced. 'poedex price' is how you ask about one."
        ),
    )
    appraise.add_argument("--character", help="character name (default: most recently played)")
    appraise.add_argument("--force", action="store_true", help="ignore the inventory cache TTL")
    appraise.add_argument(
        "--refresh-prices", action="store_true", help="re-fetch every price table first"
    )
    appraise.add_argument(
        "--threshold",
        type=float,
        default=None,
        help=(
            "keep threshold in chaos for this run only, overriding the "
            "appraisal.keep_threshold_chaos setting (default 20)"
        ),
    )
    appraise.add_argument(
        "--strictness",
        choices=[level.value for level in Strictness],
        default=None,
        help=(
            "tier-2 gate bias. 'generous' (the default, and what a bag wants) errs "
            "toward flagging; 'strict' is the stash bias and accepts only hard "
            "requirements"
        ),
    )
    appraise.add_argument(
        "--all", action="store_true", help="list the trash rows instead of collapsing them"
    )
    _add_league_argument(appraise)

    stash = sub.add_parser(
        "stash",
        help="the stash: tab list, one tab, and the crawl",
        description=(
            "Stash tabs are one request each and there is no batch endpoint, so this "
            "command is organised by what each thing costs. The bare form lists tabs "
            "and spends nothing but the tab list; 'tab N' reads one tab; 'crawl' "
            "reads everything and refuses to start without --yes. Remove-only tabs "
            "are fetched once and cached forever — they cannot gain items — which is "
            "why a second full refresh is seconds rather than half an hour."
        ),
    )
    # Deliberately **not** argparse subparsers. A subparser re-applies its own
    # defaults over the parent namespace, so `poedex stash --league Standard tab 3`
    # would silently drop the league — and a silently wrong league is the most
    # expensive bug this project has had (see `_add_league_argument`). One flat parser
    # accepts the flag on either side of the action.
    stash.add_argument(
        "action",
        nargs="?",
        choices=["list", "tab", "plan", "crawl"],
        default="list",
        help="what to do. Default: list the tabs",
    )
    stash.add_argument(
        "index",
        nargs="?",
        type=int,
        default=None,
        help="which tab, for 'tab'. The index 'poedex stash' prints",
    )
    stash.add_argument(
        "--force",
        action="store_true",
        help="ignore the cache: re-fetch the tab list, or the tab being read",
    )
    stash.add_argument(
        "--strictness",
        choices=[level.value for level in Strictness],
        default=None,
        help="override the tier-2 gate for this run. The stash default is 'strict'",
    )
    stash.add_argument(
        "--all", action="store_true", help="list the trash rows instead of collapsing them"
    )
    stash.add_argument(
        "--yes",
        action="store_true",
        help="for 'crawl': actually spend the requests it just described",
    )
    stash.add_argument(
        "--restart",
        action="store_true",
        help="for 'crawl': ignore the saved progress and walk every tab again",
    )
    stash.add_argument(
        "--limit", type=int, default=None, help="for 'crawl': stop after this many tabs"
    )
    _add_league_argument(stash)

    price = sub.add_parser(
        "price",
        help="ask the market about one item, with the mods you choose",
        description=(
            "Prints the item's mods with their real tier on this base, then runs a "
            "trade search built from the ones you picked. Without --mods it asks "
            "about the pre-ticked set — the rolls the mod database says are top tier "
            "here — and --dry-run prints the list and the query without spending a "
            "request. Two trade requests per real check, and no automatic "
            "broadening: 'nothing matched what you ticked' is an answer."
        ),
    )
    price.add_argument(
        "uid", help="which item: a name, a uid, or enough of either to be unambiguous"
    )
    price.add_argument("--character", help="character name (default: most recently played)")
    price.add_argument(
        "--tab",
        type=int,
        default=None,
        help=(
            "look for the item in this stash tab instead of the bag. The same check, "
            "on an item that lives somewhere else — 'poedex stash tab N' lists uids"
        ),
    )
    price.add_argument(
        "--mods",
        default=None,
        help=(
            "comma-separated mod indexes to filter on, as listed by --dry-run. "
            "Omitted, the pre-ticked set is used; '' asks about none of them"
        ),
    )
    price.add_argument(
        "--open-prefixes",
        type=int,
        default=None,
        help="require at least N free prefix slots — a real filter, and real crafting value",
    )
    price.add_argument(
        "--open-suffixes", type=int, default=None, help="require at least N free suffix slots"
    )
    price.add_argument(
        "--dry-run",
        action="store_true",
        help="print the mod list and the query that would run, and spend nothing",
    )
    _add_league_argument(price)

    sub.add_parser("limits", help="print the rate limiter's current view")

    moddb = sub.add_parser(
        "moddb",
        help="the mod database: its age, a base, or what a rolled mod is",
        description=(
            "Prints the database's game version and build date first, always. A mod "
            "database one league old does not fail — it answers confidently and "
            "wrongly — so its age is the headline, not a footnote."
        ),
    )
    moddb.add_argument("--base", help="a base type, e.g. 'Hubris Circlet'")
    moddb.add_argument("--mod", help="a rolled mod line, e.g. '+95 to maximum Life'")
    moddb.add_argument("--ilvl", type=int, default=0, help="the item's level")
    moddb.add_argument(
        "--origin",
        default=ModOrigin.EXPLICIT.value,
        choices=[o.value for o in ModOrigin],
        help="which array the line came from",
    )
    moddb.add_argument(
        "--influence",
        action="append",
        choices=["shaper", "elder", "crusader", "hunter", "redeemer", "warlord"],
        help="influences on the item; repeatable. Without it, influence mods cannot match",
    )

    serve = sub.add_parser(
        "serve",
        help="run the web surface on http://127.0.0.1:7331",
        description=(
            "Starts the runtime and serves the bag screen over HTTP. Binds to the "
            "loopback interface only and refuses anything else: this exposes your "
            "account's inventory, and 0.0.0.0 would put it on whatever network you "
            "happen to be attached to."
        ),
    )
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port (default 7331)")
    serve.add_argument(
        "--character",
        default=None,
        help=(
            "read this character instead of whoever you played most recently. "
            "Applies to this run only; 'poedex config set poeapi.character <name>' "
            "makes it stick"
        ),
    )
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        # Accepted so the refusal is reachable and testable, never advertised.
        help=argparse.SUPPRESS,
    )
    serve.add_argument(
        "--static",
        default=None,
        help="serve this directory as the SPA instead of surfaces/web/dist",
    )

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
    freshness.add_argument("--seconds", type=float, default=DEFAULT_SECONDS, help="how long to run")
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
    # Blank is not "broken" any more: the name is read off the profile the first
    # time a request needs one, so an unattributed credential is merely young.
    print(f"account:    {status.account or '- (read from your profile on first use)'}")
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
        print("for this run:     poedex gamelog watch --path /path/to/Client.txt")
        print("to make it stick: poedex config set gamelog.log_path /path/to/Client.txt")


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
    elif args.command == "sync":

        async def runner(registry: Registry) -> int:
            return await cmd_sync(
                registry.api(PoeApi),
                character=args.character,
                refresh=args.force,
                equipment=args.equipment,
            )

    elif args.command == "value":

        async def runner(registry: Registry) -> int:
            return await cmd_value(
                registry.api(PricesApi),
                registry.api(PoeApi),
                character=args.character,
                refresh=args.force,
                refresh_prices=args.refresh_prices,
                league=args.league,
            )

    elif args.command == "appraise":

        async def runner(registry: Registry) -> int:
            return await cmd_appraise(
                registry.api(AppraisalApi),
                registry.api(PoeApi),
                registry.api(PricesApi),
                character=args.character,
                refresh=args.force,
                refresh_prices=args.refresh_prices,
                strictness=args.strictness,
                threshold=args.threshold,
                show_all=args.all,
                league=args.league,
            )

    elif args.command == "stash":

        async def runner(registry: Registry) -> int:
            appraisal = registry.api(AppraisalApi)
            prices = registry.api(PricesApi)
            if args.action == "tab":
                if args.index is None:
                    print("which tab? 'poedex stash tab N'", file=sys.stderr)
                    return 2
                return await cmd_stash_tab(
                    appraisal,
                    prices,
                    args.index,
                    league=args.league,
                    strictness=args.strictness,
                    refresh=args.force,
                    show_all=args.all,
                )
            if args.action == "plan":
                return await cmd_stash_plan(appraisal, league=args.league)
            if args.action == "crawl":
                return await cmd_stash_crawl(
                    registry.api(PoeApi),
                    appraisal,
                    league=args.league,
                    yes=args.yes,
                    resume=not args.restart,
                    limit=args.limit,
                )
            return await cmd_stash_list(
                appraisal, prices, league=args.league, refresh=args.force
            )

    elif args.command == "price":

        async def runner(registry: Registry) -> int:
            return await cmd_price(
                registry.api(AppraisalApi),
                registry.api(PoeApi),
                registry.api(PricesApi),
                uid=args.uid,
                character=args.character,
                tab_index=args.tab,
                mods=args.mods,
                open_prefixes=args.open_prefixes,
                open_suffixes=args.open_suffixes,
                dry_run=args.dry_run,
                league=args.league,
            )

    elif args.command == "config":

        async def runner(registry: Registry) -> int:
            return await cmd_config(
                registry,
                action=args.config_command,
                key=getattr(args, "key", None),
                value=getattr(args, "value", None),
                verbose=getattr(args, "verbose", False),
            )

    elif args.command == "characters":

        async def runner(registry: Registry) -> int:
            return await cmd_characters(registry.api(PoeApi), refresh=args.force)

    elif args.command == "limits":
        runner = cmd_limits
    elif args.command == "moddb":

        async def runner(registry: Registry) -> int:
            return await cmd_moddb(
                registry,
                base=args.base,
                mod=args.mod,
                ilvl=args.ilvl,
                origin=args.origin,
                influence=args.influence,
            )

    elif args.command == "serve":
        # Set before _with_runtime builds the registry: poeapi reads it at resolve
        # time, and a flag must not persist into the user's settings file.
        if args.character:
            os.environ[CHARACTER_ENV] = args.character

        async def runner(registry: Registry) -> int:
            return await cmd_serve(registry, host=args.host, port=args.port, static_dir=args.static)

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
