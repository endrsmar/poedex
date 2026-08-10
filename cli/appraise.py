"""`poedex appraise` — Phase 4's exit criterion, and the thing being judged.

`poedex value` prints a priced bag. This prints an *answer*. The difference is what
the whole project is for: at 300 px on a Steam Deck, in gaming mode, the player is
not going to read a price list — they need to know whether to walk to the stash, and
which slots to grab on the way.

So the output is organised by decision, not by value:

* **Blocks, in the order the player acts on them.** ``KEEP`` first, then ``CHECK``,
  then ``UNPRICEABLE``, then ``TRASH``. Each block has its own subtotal, so "is the
  trip worth it" is answerable from the first two lines.
* **``TRASH`` is collapsed.** It is the largest block on a real bag and the least
  informative — its whole content is "and the rest is worth 3 chaos". ``--all``
  expands it. Nothing is hidden without saying how much was hidden.
* **``UNPRICEABLE`` is a block, never a footnote.** Its rows carry a unit count and
  the total line says the money is excluded. Four states in, four states out.
* **A verdict glyph as well as a word.** SPEC §5.4 asks for shape *and* colour so
  the grid survives greyscale; a terminal that is not a TTY gets no colour, and the
  glyph and the word carry the whole meaning on their own.
* **The gate's reasoning is printed.** "check — influenced, ilvl 86 base" is a
  falsifiable claim the player can disagree with. "check" on its own is an oracle,
  and an oracle nobody can argue with is one nobody trusts either.
"""

from __future__ import annotations

import os
import sys

from cli.value import format_chaos, prepare_league
from modules.appraisal.backend.api import (
    AppraisalApi,
    BagAppraisal,
    ItemVerdict,
    Strictness,
    Verdict,
)
from modules.poeapi.backend.api import PoeApi, Source
from modules.prices.backend.api import PricesApi

MAX_NAME = 28
MAX_REASON = 58
RULE = "─" * 74

# Shape as well as colour (SPEC §5.4). The glyphs are chosen to differ in ink
# density as well as outline, so they stay distinguishable in a screenshot, in a
# monochrome terminal, and to a colourblind reader.
GLYPH: dict[Verdict, str] = {
    Verdict.KEEP: "●",  # ● filled
    Verdict.CHECK: "◐",  # ◐ half
    Verdict.UNPRICEABLE: "?",
    Verdict.TRASH: "·",  # · dot
}

COLOUR: dict[Verdict, str] = {
    Verdict.KEEP: "\033[1;32m",
    Verdict.CHECK: "\033[1;33m",
    Verdict.UNPRICEABLE: "\033[1;35m",
    Verdict.TRASH: "\033[2;37m",
}
RESET = "\033[0m"

BLOCK_ORDER = (Verdict.KEEP, Verdict.CHECK, Verdict.UNPRICEABLE, Verdict.TRASH)

HEADLINE: dict[Verdict, str] = {
    Verdict.KEEP: "worth the trip",
    Verdict.CHECK: "look before you vendor",
    Verdict.UNPRICEABLE: "not in the price index — not worthless",
    Verdict.TRASH: "vendor",
}


def use_colour(stream=None) -> bool:
    """Colour only for a real terminal, and never when ``NO_COLOR`` is set.

    Piping the output into a file or a test must produce the same characters a human
    reads, or the assertions are testing the escape codes rather than the report.
    """
    if os.environ.get("NO_COLOR"):
        return False
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


def paint(text: str, verdict: Verdict, *, colour: bool) -> str:
    return f"{COLOUR[verdict]}{text}{RESET}" if colour else text


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def render_row(item: ItemVerdict, *, colour: bool) -> str:
    """One item: glyph, name, stack, line total, and the reason for the verdict.

    The value column reads ``—`` for anything with no price at all, which includes
    every gate-flagged rare. Printing ``0c`` next to "look before you vendor" was the
    first draft, and it is a lie in the one place the output most needs to be honest:
    the item is not worth nothing, it is worth *unknown*, and that is why it is on
    the list.
    """
    glyph = paint(GLYPH[item.verdict], item.verdict, colour=colour)
    name = _clip(item.name, MAX_NAME)
    stack = f"x{item.stack_size}" if item.stack_size > 1 else ""
    unpriced = item.valuation.unpriceable
    value = "     —" if unpriced else f"{format_chaos(item.total_chaos):>6}c"
    return f"  {glyph} {name:<{MAX_NAME}} {stack:>6} {value}   {_clip(item.reason, MAX_REASON)}"


def block_total(items: list[ItemVerdict]) -> float:
    return sum(item.total_chaos for item in items if not item.unpriceable)


def render_block(
    verdict: Verdict,
    items: list[ItemVerdict],
    *,
    colour: bool,
    collapsed: bool = False,
) -> list[str]:
    """One verdict's heading, subtotal, and rows — or a one-line summary."""
    if not items:
        return []
    label = paint(f"{verdict.value.upper():<12}", verdict, colour=colour)
    if verdict is Verdict.UNPRICEABLE:
        units = sum(item.stack_size for item in items)
        head = f"{label}{len(items)} row(s), {units} unit(s)   {HEADLINE[verdict]}"
    else:
        head = (
            f"{label}{len(items)} item(s), {format_chaos(block_total(items))}c"
            f"   {HEADLINE[verdict]}"
        )
    lines = [head]
    if collapsed:
        lines.append(f"    ({len(items)} rows hidden — pass --all to list them)")
    else:
        lines.extend(render_row(item, colour=colour) for item in items)
    lines.append("")
    return lines


def render_appraisal(result: BagAppraisal, *, show_all: bool = False, colour: bool = False) -> str:
    """The four blocks, in the order the player acts on them."""
    ranked = result.ranked()
    lines: list[str] = []
    for verdict in BLOCK_ORDER:
        items = [item for item in ranked if item.verdict is verdict]
        collapsed = verdict is Verdict.TRASH and not show_all and len(items) > 3
        lines.extend(render_block(verdict, items, colour=colour, collapsed=collapsed))
    if not any(line.strip() for line in lines):
        lines = ["  (the bag is empty)", ""]
    return "\n".join(lines).rstrip("\n")


def render_summary(result: BagAppraisal, *, colour: bool = False) -> str:
    """The bottom line. Four counts, a total, and what the total leaves out."""
    counts = result.counts
    tally = "  ".join(
        paint(f"{verdict.value} {counts[verdict.value]}", verdict, colour=colour)
        for verdict in BLOCK_ORDER
    )
    money = [f"{format_chaos(result.total_chaos)} chaos"]
    if result.total_divine is not None:
        money.append(f"{result.total_divine:,.2f} divine")

    lines = [RULE, "bag total:  " + "  ·  ".join(money)]
    unpriceable = result.of(Verdict.UNPRICEABLE)
    if unpriceable:
        lines.append(
            f"            excludes {len(unpriceable)} unpriceable row(s), "
            f"{result.unpriceable_stack} unit(s) — the total is a floor, not a value"
        )
    lines.append("verdicts:   " + tally)
    candidates = result.escalation_candidates
    if candidates:
        lines.append(
            f"tier 3:     {len(candidates)} item(s) the gate would query, "
            f"1 request each — on demand only, none spent here"
        )
    return "\n".join(lines)


def render_header(
    result: BagAppraisal,
    *,
    character: str | None,
    rows: int,
    league: str | None = None,
) -> str:
    """The six facts a verdict list is only meaningful against.

    ``league`` is the resolved description — "Allflame (from the character)" — not a
    bare name. Every chaos figure below it is denominated in that league's economy,
    and the player is the only one who can tell whether it is the right one.
    """
    table = result.table
    if table is None:
        tables = "unknown"
    else:
        when = table.newest.isoformat(timespec="seconds") if table.newest else "never"
        tables = f"{table.loaded}/{table.requested} loaded for {table.league or '-'}, newest {when}"
        tables += " (STALE)" if table.stale else " (ok)"
        if table.note:
            tables += f"\n            {table.note}"
    context = "bag" if result.strictness is Strictness.GENEROUS else "stash"
    gated = [item for item in result.items if item.gate.considered]
    flagged = len(result.escalation_candidates)
    return "\n".join(
        [
            f"character:  {character or '-'}",
            f"league:     {league or result.league}",
            f"keep at:    {format_chaos(result.threshold_chaos)} chaos",
            f"tables:     {tables}",
            f"items:      {rows} row(s), {result.lookups} price lookup(s)",
            f"gate:       {result.strictness.value} ({context}) — "
            f"{len(gated)} row(s) reached tier 2, {flagged} flagged",
        ]
    )


async def cmd_appraise(
    appraisal: AppraisalApi,
    poeapi: PoeApi,
    prices: PricesApi,
    *,
    character: str | None,
    refresh: bool,
    refresh_prices: bool = False,
    strictness: str | None = None,
    threshold: float | None = None,
    show_all: bool = False,
    colour: bool | None = None,
    league: str | None = None,
) -> int:
    bag = await poeapi.get_items(character, refresh=refresh)
    choice = await prepare_league(prices, bag.league, override=league)
    if refresh_prices:
        await prices.refresh(force=True, league=choice.league)
    items = bag.by_source(Source.BAG)
    result = await appraisal.appraise(
        items,
        strictness=Strictness(strictness) if strictness else None,
        threshold_chaos=threshold,
        league=bag.league,
        override=league,
    )
    painted = use_colour() if colour is None else colour

    print(
        render_header(
            result, character=bag.character, rows=len(items), league=choice.describe()
        )
    )
    print()
    print(render_appraisal(result, show_all=show_all, colour=painted))
    print()
    print(render_summary(result, colour=painted))
    print(f"trade:      {result.trade_requests} request(s) — tier 3 is on demand only")

    if bag.meta.stale:
        print("\nthis is cached inventory data; nothing was fetched", file=sys.stderr)
        return 1
    if result.table is not None and result.table.loaded == 0:
        print(
            "\nno price tables loaded — every verdict below is the gate's alone",
            file=sys.stderr,
        )
        return 1
    return 0
