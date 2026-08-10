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

**The pre-tick is a proposal about *rolls*, not about value.** A line is *eligible*
when the roll is in the best tier its group reaches on this base, or when it came out
of an influence pool. Everything else starts unticked, including lines the database
could not read: an unknown line might be the best mod on the item, and pre-ticking it
would put a filter nobody chose into a query somebody else's number comes out of.

**At most :data:`MAX_PRETICKED` of the eligible rows are actually ticked, best
first.** Phase 9b measured the pre-tick over twenty real rares and found it failing
hardest on the item worth the most: 2-divine Soldier Gloves with six T1/T2 rolls got
**6 of 6**, and a manual check never broadens, so the default button press sent
exactly the six-filter conjunction Phase 9 had already measured returning zero
listings. The pre-tick was worst precisely where the item was best, which is the
opposite of its purpose.

Of the three fixes §5b offered this is the **cap**, and the reason is asymmetry: a
short default query returns a *superset* the player narrows with one more tick, where
an empty one reports "no listings" about a two-divine item and offers no way back.
The other two are worse trades. A warning before the request still spends the request
and still reports zero under the player's heading. An explicit broadening step is the
thing §5b deleted on purpose — broadening answers a different question, and giving it
a button does not change whose heading the answer arrives under. Nothing here disables
a row, and :attr:`ItemHighlight.note` says how many eligible rows were left unticked,
so a cap that bit is visible before the button rather than after the answer.

:func:`significance` is the order, and it ranks on facts `moddb` already has — never
on a list of mod names, which would be ``MOD_GROUPS`` coming back through the door
this project just spent a phase closing. The strongest of those facts is the **item
level the game demands for the roll**: GGG gates its deepest affixes behind item
level, so the lowest ``required_level`` among the surviving candidates is the game's
own statement of how far up a ladder a roll had to reach, and it needs no opinion
about the market at all. Ties break on how close the roll came to the best that base
can do, then on ladder depth, then on the item's own line order.

**A line the offline bridge cannot name says so, and stays tickable.**
``tradeable`` is `moddb`'s text→trade-id bridge. Phase 9 called it "a trimmed
artifact with real holes" and named ``98% increased Energy Shield`` as one; Phase 9b
measured the holes at 12.9% of mod lines, found that most of them were the *local*
reading of a sentence rather than a missing sentence, and closed them — that mod now
resolves, and coverage is 96.7%. What is left is genuinely absent from GGG's own
filter list, which makes the annotation mean something it did not mean before: not
"our copy is thin here" but "the trade site has no filter for this".

It still annotates rather than disables. The live stat document is the authority, and
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
from modules.moddb.backend.api import (
    Attribution,
    ModDbApi,
    ModMatch,
    ModReport,
    Origin,
)
from modules.poeapi.backend.api import NormalizedItem

__all__ = [
    "MAX_PRETICKED",
    "build",
    "eligible_count",
    "options_for",
    "preticked_indexes",
    "significance",
    "tier_label",
]

MAX_PRETICKED = 2
"""How many rows the default button press is allowed to AND together.

**Two, and it was measured on the item the defect was measured on.** The obvious cap
was three — Phase 9b's median was 3 and §5b called that fine — so it was run against
the live API on the fixture's 2-divine Soldier Gloves, ANDing the top-ranked filters
with the same widened floors the panel would send:

===========  =========
filters      listings
===========  =========
6 (before)   0
3            0
2            **3**
1            35
===========  =========

Three is not a smaller version of the bug, it is the same bug: an empty search that
reads as "worthless" about a two-divine item. The median being 3 was an observation
about what the pre-tick *did*, never a measurement that 3 finds anything, and this is
the difference between the two. Two also agrees with the number Phase 9 measured
independently for the automatic path — ``trade.MAX_STAT_FILTERS`` is 2 because two
filters returned 2 524, 198 and 438 listings where every mod ANDed returned 0, 0 and 1.

The direction of error is chosen deliberately. Two filters return a **superset** the
player narrows with one more tick; six return nothing and offer no way back, and the
player cannot tell which filter emptied the search. Nothing here disables a row, and
:attr:`ItemHighlight.note` says how many eligible rolls were left unticked.

It is a ceiling on a *proposal*, not on a query. :data:`MAX_QUERY_FILTERS` is still 6:
a player who ticks all six has asked for a near-exact-match search and gets one.
"""


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


def _eligible(match: ModMatch) -> bool:
    """Whether this row is a candidate for the pre-tick at all — unchanged since
    Phase 9. An influence roll, or a roll in the best tier its group reaches here."""
    if match.is_influence_mod:
        return True
    return high_tier(match, strictness=Strictness.GENEROUS)


def _affix_level(match: ModMatch) -> int:
    """The item level this roll needed, as low as the evidence allows.

    ``min`` over the survivors rather than ``max`` on purpose. Where attribution is
    :data:`Attribution.GROUP` the roll fits several tiers and the honest claim is the
    cheapest one that explains it — saying a roll "needed ilvl 84" when a tier at 68
    also produces it would be reading a ladder position off an uncertainty.
    """
    levels = [candidate.required_level for candidate in match.candidates]
    return min(levels) if levels else 0


def _ceiling_fraction(match: ModMatch) -> float:
    """How much of the best this base can do the roll actually got. ``0.0`` unknown.

    Only meaningful where `moddb` named a ceiling, which it does only when the group
    is certain — a ceiling averaged over two possible groups is a number with no
    meaning, and the property already refuses to produce one.
    """
    value, ceiling = match.value, match.ceiling
    if value is None or not ceiling:
        return 0.0
    return abs(value) / abs(ceiling)


def significance(match: ModMatch) -> tuple[int, int, float, int]:
    """How strong a claim `moddb` can make about this roll. Higher sorts first.

    Four facts, in order, and **no mod names**. §5b's own conclusion is that which
    mods matter is player knowledge; this does not try to know it. What it ranks is
    how far up the game's own ladders a roll had to reach, which is a different
    question with an answer in the data:

    1. **It came out of an influence pool.** The rarest thing `moddb` can say about a
       line, and §5b's fourth highlight criterion.
    2. **The item level the game demands for it** (:func:`_affix_level`). GGG gates
       its deepest affixes behind item level, so this is the game's own ordering of
       how much of an item's level budget a roll consumed.
    3. **How close it came to this base's ceiling** (:func:`_ceiling_fraction`).
    4. **Ladder depth.** T1 of 11 beats ten other tiers; T1 of 2 beats one.

    Deliberately *not* here: the tier number on its own. Every eligible row is already
    at or near the top of its ladder, so tier alone cannot separate them, and it is
    the one number that made "T1 of 2 stun recovery" look like "T1 of 11 life".
    """
    return (
        int(match.is_influence_mod),
        _affix_level(match),
        _ceiling_fraction(match),
        match.tiers or 0,
    )


def preticked_indexes(
    matches: Sequence[tuple[int, ModMatch]], *, limit: int = MAX_PRETICKED
) -> frozenset[int]:
    """Which rows start ticked: the ``limit`` most significant eligible ones.

    ``matches`` is ``(row index, match)`` in the item's own order, which is also the
    final tie-break — two rows the four facts cannot separate stay in the order the
    game printed them, so the same item always proposes the same query.
    """
    eligible = [(index, match) for index, match in matches if _eligible(match)]
    eligible.sort(key=lambda pair: (significance(pair[1]), -pair[0]), reverse=True)
    return frozenset(index for index, _match in eligible[: max(0, limit)])


def eligible_count(report: ModReport | None) -> int:
    """How many rows the pre-tick *would* have ticked before the cap."""
    if report is None:
        return 0
    return sum(
        1
        for match in report.matches
        if (match.origin.is_affix or match.origin is Origin.IMPLICIT) and _eligible(match)
    )


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

    rows = [
        (index, match)
        for index, match in enumerate(report.matches)
        # Enchants are not affixes and are not priced as mods on a rare.
        if match.origin.is_affix or match.origin is Origin.IMPLICIT
    ]
    ticked = preticked_indexes(rows)

    options: list[ModOption] = []
    for index, match in rows:
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
                preticked=index in ticked,
                higher_is_better=match.higher_is_better,
                local=match.local,
                tradeable=_tradeable(match.text, match.origin, moddb, match.local),
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
            value=(value := _first_number(text)),
            # No database, so no ladder to read a direction off — but the roll's own
            # sign is still a fact, and it is the one that decides whether the filter
            # is a floor or a ceiling.
            higher_is_better=value is None or value >= 0,
            tradeable=_tradeable(text, origin, moddb),
        )
        for index, (origin, text) in enumerate(lines)
    ]


def _first_number(text: str) -> float | None:
    """The leading roll on a line the database could not read — **with its sign**.

    ``-9 to Total Mana Cost of Skills`` is ``-9.0``, not ``9.0``. Dropping the minus
    here is how a reduced-cost roll used to reach :meth:`Selection.spec` as a positive
    number and come out the other side as a filter for the opposite item.
    """
    digits = ""
    for index, char in enumerate(text):
        if char.isdigit() or (char == "." and digits):
            if not digits and index and text[index - 1] == "-":
                digits = "-"
            digits += char
        elif digits:
            break
    return float(digits) if digits and digits != "-" else None


def _tradeable(
    text: str, origin: Origin, moddb: ModDbApi | None, local: bool | None = None
) -> bool:
    """Whether the trade API has an id for this sentence, asked offline.

    `moddb` carries the bridge, so the panel can annotate a row without a network
    round trip and without `prices` having fetched its 2 MB stat document yet. Neither
    answer is a promise about the live document, which is why this only ever adds a
    note: :func:`modules.prices.backend.trade.build_plan` drops what it cannot resolve
    and says how many it dropped.

    ``local`` is passed through because the same sentence can have an id in one
    reading and not the other, and asking the wrong one would put "cannot be searched"
    on a row that searches fine — a false alarm on a panel is how a true alarm stops
    being read.
    """
    if moddb is None:
        return True
    try:
        return moddb.trade_stat_id(text, origin=origin, local=local) is not None
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
    options = options_for(item, report, moddb=moddb)
    return ItemHighlight(
        uid=item.uid,
        name=item.name or item.base_type,
        base_type=item.base_type,
        rarity=item.rarity.value,
        ilvl=item.ilvl,
        gate=gate,
        mods=options,
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
            else _note(report, options)
        ),
    )


def _note(report: ModReport | None, options: Sequence[ModOption]) -> str:
    """How much the database read, plus what the pre-tick cap held back.

    The held-back count is said *before* the button, not after the answer, because
    that is the whole difference between a proposal and a surprise: "three of six
    high-tier rolls ticked" is a player deciding how wide to search, and a query
    description explaining it afterwards is a receipt for a request already spent.
    """
    parts = [describe_report(report)]
    held = eligible_count(report) - sum(1 for option in options if option.preticked)
    if held > 0:
        parts.append(f"{held} more high-tier {'roll' if held == 1 else 'rolls'} left unticked")
    return ", ".join(parts)


def selected_texts(options: Sequence[ModOption], indexes: Sequence[int]) -> list[str]:
    """The mod lines behind a set of ticks, for a description or a log line."""
    by_index = {option.index: option for option in options}
    return [by_index[i].text for i in indexes if i in by_index]
