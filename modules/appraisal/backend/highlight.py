"""The checkbox list: an item's mods as rows a player can tick.

This is the other half of IMPLEMENTATION-PLAN §5b. :mod:`.gate` decides an item is
worth *asking* about; this turns the item into the question, and the player answers
it. Nothing here builds a query — :meth:`Selection.spec` does that, from the ticks.

Three rules, and all three are about not overclaiming.

**A tier is shown only where `moddb` asserted one.** :data:`Attribution.EXACT` gets
``T4 of 10``; :data:`Attribution.GROUP` gets ``T2-T3 of 8``, which is a range and
reads as one; :data:`Attribution.AMBIGUOUS` and :data:`Attribution.UNKNOWN` get
:data:`TIER_UNKNOWN` and no number at all. That last case is roughly one affix line in
five on the live-derived fixtures, and a list that showed "T2" for it would be right
most of the time — which is exactly what makes it dangerous, because the player
cannot tell which fifth they are looking at.

**The pre-tick is a proposal about *rolls*, not about value.** A line is pre-ticked
when the roll is in the best tier its group reaches on this base, or when it came out
of an influence pool. Everything else starts unticked, including lines the database
could not read: an unknown line might be the best mod on the item, and pre-ticking it
would put a filter nobody chose into a query somebody else's number comes out of.

**A line the offline bridge cannot name says so, and stays tickable.**
``tradeable`` is `moddb`'s text→trade-id bridge, which is a trimmed artifact with
real holes — ``98% increased Energy Shield`` is a common mod it has no entry for. So
it annotates rather than disables: the live stat document is the authority, and
``build_plan`` drops what it cannot resolve and reports the count in the query
description. Greying out a filter that would have worked is the same mistake as
building the query for the player, in the other direction.
"""

from __future__ import annotations

from collections.abc import Sequence

from modules.appraisal.backend.api import (
    TIER_UNKNOWN,
    GateResult,
    ItemHighlight,
    ModOption,
    Strictness,
)
from modules.appraisal.backend.gate import describe_report, high_tier
from modules.moddb.backend.api import Attribution, ModDbApi, ModMatch, ModReport, Origin
from modules.poeapi.backend.api import NormalizedItem

__all__ = ["build", "options_for", "tier_label"]


def tier_label(match: ModMatch) -> str:
    """``T4 of 10``, ``T2-T3 of 8``, or ``unknown`` — never a guess.

    :meth:`ModMatch.describe` is the source of the confident wordings, so the panel
    and ``poedex moddb`` cannot disagree about what was claimed. The two unconfident
    states are collapsed to one word here on purpose: "tier unknown" and "unknown
    mod" are a distinction for a developer reading a log, and at 300 px both mean
    *do not believe a tier about this line*.
    """
    if not match.attribution.is_confident:
        return TIER_UNKNOWN
    return match.describe()


def _preticked(match: ModMatch) -> bool:
    if match.is_influence_mod:
        return True
    return high_tier(match, strictness=Strictness.GENEROUS)


def options_for(
    item: NormalizedItem,
    report: ModReport | None,
    *,
    moddb: ModDbApi | None = None,
) -> list[ModOption]:
    """One row per readable line of the item, in the item's own order.

    Falls back to the raw mod text when there is no database: without `moddb` the
    tool still knows the item has a ``+95 to maximum Life``, and a checkbox list of
    untiered lines is a worse offer than a tiered one but a much better one than an
    empty panel. Every row is then ``unknown`` and nothing is pre-ticked, which is the
    honest reading of "we have no idea which of these matters".
    """
    if report is None:
        return _untiered(item, moddb)

    options: list[ModOption] = []
    for index, match in enumerate(report.matches):
        if not match.origin.is_affix and match.origin is not Origin.IMPLICIT:
            # Enchants are not affixes and are not priced as mods on a rare.
            continue
        affix = match.affix
        options.append(
            ModOption(
                index=index,
                text=match.text,
                origin=match.origin.value,
                affix=affix.value if affix is not None else None,
                tier=match.tier,
                tiers=match.tiers,
                tier_label=tier_label(match),
                attribution=match.attribution.value,
                top_tier=match.top_group,
                value=match.value,
                ceiling=match.ceiling,
                influences=tuple(sorted(pool.value for pool in match.influences)),
                preticked=_preticked(match),
                tradeable=_tradeable(match.text, match.origin, moddb),
            )
        )
    return options


def _untiered(item: NormalizedItem, moddb: ModDbApi | None) -> list[ModOption]:
    lines: list[tuple[Origin, str]] = [
        *((Origin.IMPLICIT, text) for text in item.mods.implicit),
        *((Origin.EXPLICIT, text) for text in item.mods.explicit),
        *((Origin.FRACTURED, text) for text in item.mods.fractured),
        *((Origin.CRAFTED, text) for text in item.mods.crafted),
    ]
    return [
        ModOption(
            index=index,
            text=text,
            origin=origin.value,
            tier_label=TIER_UNKNOWN,
            attribution=Attribution.UNKNOWN.value,
            value=_first_number(text),
            tradeable=_tradeable(text, origin, moddb),
        )
        for index, (origin, text) in enumerate(lines)
    ]


def _first_number(text: str) -> float | None:
    digits = ""
    for char in text:
        if char.isdigit() or (char == "." and digits):
            digits += char
        elif digits:
            break
    return float(digits) if digits else None


def _tradeable(text: str, origin: Origin, moddb: ModDbApi | None) -> bool:
    """Whether the trade API has an id for this sentence, asked offline.

    `moddb` carries ``stat_translations.json``'s bridge, so the panel can annotate a
    row without a network round trip and without `prices` having fetched its 400 kB
    stat document yet. Neither answer is a promise about the live document, which is
    why this only ever adds a note: :func:`modules.prices.backend.trade.build_plan`
    drops what it cannot resolve and says how many it dropped.
    """
    if moddb is None:
        return True
    try:
        return moddb.trade_stat_id(text, origin=origin) is not None
    except Exception:  # pragma: no cover - a broken artifact must not disable ticking
        return True


def build(
    item: NormalizedItem,
    gate: GateResult,
    report: ModReport | None,
    *,
    moddb: ModDbApi | None = None,
) -> ItemHighlight:
    """Assemble the proposal for one item."""
    base = report.base if report is not None else None
    return ItemHighlight(
        uid=item.uid,
        name=item.name or item.base_type,
        base_type=item.base_type,
        rarity=item.rarity.value,
        ilvl=item.ilvl,
        gate=gate,
        mods=options_for(item, report, moddb=moddb),
        open_prefixes=report.open_prefixes if report is not None else 0,
        open_suffixes=report.open_suffixes if report is not None else 0,
        max_prefixes=report.max_prefixes if report is not None else 3,
        max_suffixes=report.max_suffixes if report is not None else 3,
        counts_are_certain=report.counts_are_certain if report is not None else False,
        top_affix_level=base.top_affix_level if base is not None else None,
        note=(
            # describe_report only sees the report, so a None one makes it blame the
            # database. For an unidentified item the database is fine and there is
            # simply nothing to read, which is a different thing to tell the player.
            "unidentified — no mods to read until it is identified"
            if not item.identified
            else describe_report(report)
        ),
    )


def selected_texts(options: Sequence[ModOption], indexes: Sequence[int]) -> list[str]:
    """The mod lines behind a set of ticks, for a description or a log line."""
    by_index = {option.index: option for option in options}
    return [by_index[i].text for i in indexes if i in by_index]
