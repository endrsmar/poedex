"""`poedex selftest freshness` — the one test that needs a human in the game.

SPEC §4.3 concluded, from 20 samples over 9 minutes, that the character endpoint
commits at zone transitions rather than live: ~90% confidence. This command is how
the remaining 10% gets closed, and it cannot be automated — it needs someone to pick
an item up off the floor.

    poedex selftest freshness

It polls, hashes the **normalized** item set (SPEC §4.4 — raw JSON churns on fields
that have nothing to do with the inventory), and prints one row per poll with a
marker whenever the hash moves. The procedure it prints is the experiment: pick
something up mid-map, watch for ~60 s of nothing, portal to the hideout, watch the
hash change.

It is a *budget-expensive* command and says so. At the default 5 s interval it burns
through the ``30:60`` window in half a minute, at which point the limiter refuses and
the command prints the refusal rather than sleeping through it. Those refusals are
part of the output: a run that never gets refused is a run that was not polling hard
enough to prove anything.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from modules.poeapi.backend.api import PoeApi, PoeApiError, RateLimitedError

MIN_INTERVAL = 5.0
DEFAULT_INTERVAL = 5.0
DEFAULT_SECONDS = 240.0

PROCEDURE = """\
Freshness self-test — SPEC §4.3
================================

This needs you in the game. Read all of it before starting; the timing matters.

  1. Be in a MAP, mid-run, with at least one free inventory slot.
  2. Start this command. Let it print 2-3 baseline rows with an unchanged hash.
  3. PICK UP AN ITEM off the floor. Note the wall-clock time.
  4. Keep watching for ~60 seconds. EXPECTED: the hash does not move. The character
     endpoint is a snapshot committed at zone transitions, not a live read.
  5. Take a PORTAL TO YOUR HIDEOUT.
  6. Within ~0-5 seconds of loading in, the hash should change, and the item count
     should go up by one.

What each outcome means:

  * Hash moves only after the portal      -> SPEC §4.3 confirmed. Sync on zone entry.
  * Hash moves while still in the map     -> the endpoint is live after all. That
                                             invalidates the event-driven sync model
                                             in SPEC §4.4; say so before Phase 6.
  * Hash never moves                      -> wrong character, or the pickup did not
                                             land in MainInventory. Check the count.

Cost: this spends real rate-limit budget against the account, and the same POESESSID
is your live browser session. Refusals below are the limiter working, not a failure.
"""


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S")


async def cmd_freshness(
    poeapi: PoeApi,
    *,
    character: str | None,
    interval: float,
    seconds: float,
) -> int:
    interval = max(MIN_INTERVAL, interval)
    print(PROCEDURE)
    print(f"polling every {interval:.0f}s for {seconds:.0f}s — Ctrl-C to stop early")
    print()
    header = f"{'time':<10} {'poll':>4} {'items':>6} {'hash':<18} {'age':>6}  event"
    print(header)
    print("-" * len(header))

    deadline = time.monotonic() + seconds
    previous: str | None = None
    poll = 0
    changes = 0

    try:
        while time.monotonic() < deadline:
            poll += 1
            row = await _one_poll(poeapi, character, poll, previous)
            print(row.text, flush=True)
            if row.digest is not None:
                if previous is not None and row.digest != previous:
                    changes += 1
                previous = row.digest
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(interval, remaining))
    except KeyboardInterrupt:
        print("\nstopped")

    print()
    print(f"{poll} poll(s), {changes} hash change(s)")
    print(
        "Record the result — the wall-clock time of the pickup, of the portal, and of "
        "the first changed row — in docs/research-notes.md §2."
    )
    return 0


class _Row:
    __slots__ = ("digest", "text")

    def __init__(self, text: str, digest: str | None) -> None:
        self.text = text
        self.digest = digest


async def _one_poll(poeapi: PoeApi, character: str | None, poll: int, previous: str | None) -> _Row:
    try:
        result = await poeapi.get_items(character, refresh=True)
    except RateLimitedError as exc:
        return _Row(
            f"{_stamp():<10} {poll:>4} {'-':>6} {'-':<18} {'-':>6}  "
            f"REFUSED, retry in {exc.retry_after:.0f}s",
            None,
        )
    except PoeApiError as exc:
        return _Row(f"{_stamp():<10} {poll:>4} {'-':>6} {'-':<18} {'-':>6}  ERROR {exc}", None)

    digest = result.content_hash
    bag = len(result.items)
    age = (datetime.now(result.meta.fetched_at.tzinfo) - result.meta.fetched_at).total_seconds()
    if result.meta.stale:
        event = f"stale (cache); retry in {result.meta.retry_after or 0:.0f}s"
    elif previous is None:
        event = "baseline"
    elif digest != previous:
        event = "*** CHANGED ***"
    else:
        event = ""
    return _Row(
        f"{_stamp():<10} {poll:>4} {bag:>6} {digest[:16]:<18} {age:>5.0f}s  {event}",
        digest,
    )
