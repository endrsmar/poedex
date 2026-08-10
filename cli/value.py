"""`poedex value` — the bag, priced.

Phase 3's exit criterion (IMPLEMENTATION-PLAN §5). Still a dump rather than a
verdict: there are no thresholds and no keep/trash opinion here, because that is
Phase 4's `appraisal`. What this output is *for* is checking that the numbers are
believable before anything is built on them — which mostly means checking the three
places a priced bag goes wrong.

**Stacks.** ``Jeweller's Orb x2615`` and ``Divine Orb x5`` are each one row and
nothing alike, so the unit price and the line total are separate columns.

**Unpriceable.** Printed in its own block with its own count, never folded into the
total as zero. If the block is large the total below it is not the bag's worth, and
the output says so rather than leaving it to be inferred.

**Where each number came from.** The source column is ``note`` or ``bulk``; a bag
priced entirely off the player's own notes and one where the market agrees look
identical without it.
"""

from __future__ import annotations

import sys

from modules.poeapi.backend.api import PoeApi, Source
from modules.prices.backend.api import BagValuation, PricesApi, Valuation

MAX_NAME = 34


def format_chaos(value: float) -> str:
    """Chaos with a sane number of digits across nine orders of magnitude.

    A currency table spans ``0.0025c`` (an Alchemy Orb) to ``1,387,737c`` (a Mirror),
    and one format string cannot serve both without either losing the cheap items to
    ``0.00`` or drowning the expensive ones in decimals.
    """
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 10:
        return f"{value:.1f}"
    if value >= 0.1:
        return f"{value:.2f}"
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def render_row(valuation: Valuation) -> str:
    name = valuation.name
    if len(name) > MAX_NAME:
        name = name[: MAX_NAME - 1] + "…"
    stack = f"x{valuation.stack_size}" if valuation.stack_size > 1 else ""
    unit = format_chaos(valuation.price.chaos) if valuation.price else "-"
    total = format_chaos(valuation.total_chaos) if valuation.price else "-"
    detail = valuation.price.detail if valuation.price else None
    suffix = f"  ({detail})" if detail else ""
    return (
        f"  {name:<{MAX_NAME}} {stack:>8} {unit:>12}c {total:>14}c  "
        f"{valuation.source.value:<6}{suffix}"
    )


def render_unpriceable(valuation: Valuation) -> str:
    name = valuation.name
    if len(name) > MAX_NAME:
        name = name[: MAX_NAME - 1] + "…"
    stack = f"x{valuation.stack_size}" if valuation.stack_size > 1 else ""
    return f"  {name:<{MAX_NAME}} {stack:>8}   {valuation.reason or ''}"


def render_bag(result: BagValuation) -> str:
    lines: list[str] = []
    priced = sorted(result.priced, key=lambda v: -v.total_chaos)
    if priced:
        lines.append(
            f"  {'item':<{MAX_NAME}} {'stack':>8} {'unit':>13} {'total':>15}  source"
        )
        lines.extend(render_row(v) for v in priced)
    else:
        lines.append("  (nothing priced)")

    unpriceable = sorted(result.unpriceable, key=lambda v: (-v.stack_size, v.name))
    if unpriceable:
        lines.append("")
        lines.append(
            f"unpriceable — {len(unpriceable)} item(s), {result.unpriceable_stack} unit(s). "
            "Not in the index; not the same as worthless."
        )
        lines.extend(render_unpriceable(v) for v in unpriceable)
    return "\n".join(lines)


def render_total(result: BagValuation) -> str:
    parts = [f"{format_chaos(result.total_chaos)} chaos"]
    if result.total_divine is not None:
        parts.append(f"{result.total_divine:,.2f} divine")
    line = "total:      " + "  ·  ".join(parts)
    if result.unpriceable:
        line += f"   (+ {len(result.unpriceable)} unpriceable, excluded)"
    return line


def render_tables(result: BagValuation) -> str:
    table = result.table
    if table is None:
        return "tables:     unknown"
    when = table.newest.isoformat(timespec="seconds") if table.newest else "never"
    state = "STALE" if table.stale else "ok"
    line = f"tables:     {table.loaded}/{table.requested} loaded, newest {when} ({state})"
    if table.note:
        line += f"\n            {table.note}"
    return line


async def cmd_value(
    prices: PricesApi,
    poeapi: PoeApi,
    *,
    character: str | None,
    refresh: bool,
    refresh_prices: bool,
) -> int:
    if refresh_prices:
        await prices.refresh(force=True)
    bag = await poeapi.get_items(character, refresh=refresh)
    items = bag.by_source(Source.BAG)
    result = await prices.value_all(items)

    print(f"character:  {bag.character}")
    print(f"league:     {result.league}")
    print(render_tables(result))
    print(f"items:      {len(items)} row(s), {result.lookups} price lookup(s)")
    print()
    print(render_bag(result))
    print()
    print(render_total(result))
    print(f"trade:      {result.trade_requests} request(s) — tier 3 is on demand only")
    if bag.meta.stale:
        print("\nthis is cached inventory data; nothing was fetched", file=sys.stderr)
        return 1
    if result.table is not None and result.table.loaded == 0:
        print("\nno price tables loaded; every item is unpriceable", file=sys.stderr)
        return 1
    return 0
