"""`poedex price` — the manual check, on the command line.

The CLI equivalent of the checkbox list, and the shippable increment this phase is
built around: a working backend plus this command is a usable tool even with no panel
in front of it.

Two things it is careful about, both of them lessons from the two failed live runs
IMPLEMENTATION-PLAN §5b records:

* **The mod list is printed before the query is run.** The player can see which lines
  the database could attribute, what tier each roll is *on this base*, and which
  lines it refuses to name a tier for — and then choose. ``--dry-run`` stops there
  and spends nothing.
* **The comparable count is printed next to the number, always.** A median over one
  listing is one stranger's asking price wearing a median's clothes; that is what
  reported 10c for a jewel worth 1.00c across 438 comparables, and the count is the
  cheapest possible defence against believing it again.
"""

from __future__ import annotations

import sys

from cli.value import format_chaos, prepare_league
from modules.appraisal.backend.api import (
    TIER_UNKNOWN,
    AppraisalApi,
    AppraisalError,
    ItemHighlight,
    ModOption,
    PriceCheck,
    Selection,
)
from modules.poeapi.backend.api import NormalizedItem, PoeApi
from modules.prices.backend.api import PricesApi

RULE = "─" * 74


def parse_mods(raw: str | None) -> list[int] | None:
    """``"0,3,4"`` → ``[0, 3, 4]``. ``None`` means "use the pre-ticked set".

    An **empty string** is a real answer and means "none of them", which is how a
    player asks a pure open-affix question. Collapsing it into ``None`` would silently
    turn "no mods, one open prefix" into "the pre-ticked mods and one open prefix".
    """
    if raw is None:
        return None
    parts = [chunk.strip() for chunk in raw.split(",")]
    return [int(chunk) for chunk in parts if chunk]


def render_option(option: ModOption, *, ticked: bool) -> str:
    """One row of the list: the box, the index, the tier, the line."""
    box = "[x]" if ticked else "[ ]"
    tier = option.tier_label
    affix = option.affix or "?"
    ceiling = f" of {option.ceiling:g}" if option.ceiling is not None else ""
    roll = f"{option.value:g}{ceiling}" if option.value is not None else ""
    pools = f" [{'/'.join(option.influences)}]" if option.influences else ""
    # A "?" rather than a disabled row: the offline bridge has holes, and greying
    # out a filter that would have worked narrows what the player may ask.
    bridge = "" if option.tradeable else " (?)"
    return (
        f"  {box} {option.index:>2}  {tier:<12} {affix:<7} {roll:<14} "
        f"{option.text}{pools}{bridge}"
    )


def render_highlight(highlight: ItemHighlight, selection: Selection) -> str:
    """The item, why it is highlighted, and its mods as a list you can tick."""
    ticked = set(selection.mods)
    lines = [
        f"item:       {highlight.name} · {highlight.base_type}"
        + (f" · ilvl {highlight.ilvl}" if highlight.ilvl else ""),
        "highlight:  "
        + (highlight.gate.summary if highlight.highlighted else "not highlighted"),
        f"database:   {highlight.note}",
    ]
    if highlight.counts_are_certain:
        lines.append(
            f"open:       {highlight.open_prefixes} prefix(es), "
            f"{highlight.open_suffixes} suffix(es) free"
        )
    else:
        # A filter built on "1 open prefix" that is actually zero returns nothing and
        # looks like a dead search. The uncertainty has to reach whoever builds it.
        lines.append(
            "open:       affix counts uncertain — an open-affix filter may be wrong"
        )
    lines.append("")
    lines.append(f"  {'':<3} {'#':>2}  {'tier':<12} {'affix':<7} {'roll':<14} mod")
    lines.extend(render_option(option, ticked=option.index in ticked) for option in highlight.mods)
    if not highlight.mods:
        lines.append("  (no readable mods)")
    unknown = sum(1 for option in highlight.mods if option.tier_label == TIER_UNKNOWN)
    if unknown:
        # Said out loud rather than left to the reader to count. `moddb` refuses to
        # name a tier where several ladders could have produced a line, and a list
        # that showed "T2" for those would be right most of the time — which is
        # exactly what would make it dangerous.
        lines.append(
            f"\n  {unknown} line(s) show '{TIER_UNKNOWN}': the mod database will not "
            "say which mod produced them, so no tier is claimed."
        )
    untradeable = [option for option in highlight.mods if not option.tradeable]
    if untradeable:
        lines.append(
            f"  {len(untradeable)} line(s) marked '(?)' have no id in the offline "
            "bridge. Tick them anyway — the live trade document decides, and the "
            "query below says what it could not ask."
        )
    return "\n".join(lines)


def describe_selection(selection: Selection, highlight: ItemHighlight) -> str:
    spec = selection.spec(highlight)
    parts = [
        f"{focus.text} ≥ {focus.minimum:g}" if focus.minimum is not None else f"{focus.text} (any)"
        for focus in spec.mods
    ]
    if spec.open_prefixes is not None:
        parts.append(f"≥{spec.open_prefixes} open prefix(es)")
    if spec.open_suffixes is not None:
        parts.append(f"≥{spec.open_suffixes} open suffix(es)")
    return "\n".join(["query:", *(f"  · {part}" for part in parts)]) if parts else "query:  (empty)"


def render_check(result: PriceCheck) -> str:
    lines = [RULE]
    if result.priced:
        assert result.chaos is not None
        lines.append(f"price:      {format_chaos(result.chaos)} chaos   ({result.league})")
    else:
        lines.append(f"price:      —   ({result.league})")
    lines.append(f"            {result.reason}")
    if result.quote is not None and result.quote.query_url:
        lines.append(f"            {result.quote.query_url}")
    lines.append(f"trade:      {result.spent} request(s) spent")
    if result.thin:
        lines.append(
            "            too few comparables to be a market price — treat it as one "
            "seller's opinion, not a valuation"
        )
    return "\n".join(lines)


async def cmd_price(
    appraisal: AppraisalApi,
    poeapi: PoeApi,
    prices: PricesApi,
    *,
    uid: str,
    character: str | None,
    mods: str | None = None,
    open_prefixes: int | None = None,
    open_suffixes: int | None = None,
    dry_run: bool = False,
    league: str | None = None,
) -> int:
    bag = await poeapi.get_items(character)
    item = _find(bag.items, uid)
    if item is None:
        print(f"error: no item {uid!r} in the current bag", file=sys.stderr)
        return 2

    choice = await prepare_league(prices, bag.league, override=league)
    highlight = appraisal.highlight(item)
    selection = highlight.selection(
        parse_mods(mods), open_prefixes=open_prefixes, open_suffixes=open_suffixes
    )

    print(render_highlight(highlight, selection))
    print()
    print(describe_selection(selection, highlight))
    print(f"league:     {choice.describe()}")

    if dry_run:
        print("\ndry run — nothing was asked of the trade API")
        return 0
    try:
        result = await appraisal.price_check(item, selection, league=bag.league, override=league)
    except AppraisalError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 2
    print()
    print(render_check(result))
    return 0 if result.priced else 1


def _find(items: list[NormalizedItem], uid: str) -> NormalizedItem | None:
    for item in items:
        if item.uid == uid:
            return item
    return None
