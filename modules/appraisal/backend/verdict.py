"""Prices plus the gate → one verdict (SPEC §5.4).

Pure functions over a :class:`~modules.prices.backend.api.Valuation` and a
:class:`~modules.appraisal.backend.api.GateResult`. Nothing here can perform I/O or
compute a price; if a number is needed it was already asked for.

## The order, and why it is this order

::

    0.  not a loot decision at all (quest, MTX, decoration)   → not_loot
    1.  no price, and the bulk index should have carried it   → unpriceable
    1b. no price yet, tier 3 outstanding                      → check ("pricing…")
    1c. no price, tier 3 answered and found nothing           → check (terminal)
    2.  stack total at or above the keep threshold            → keep
    3.  the tier-2 gate found something                       → check
    4.  stack total at or above the check floor               → check
    5.  otherwise                                             → trash

**Not-loot is tested before everything, including unpriceable.** Every branch below
it answers "what is this worth?", and for a quest item that question has no answer to
get right or wrong: it has no market, so its absence from the price index is not a
hole, and its lack of value is not a reason to vendor it. Falling through to ``trash``
printed *vendor* next to an item that cannot be vendored — the one kind of wrong
answer that makes a player stop trusting the four that were right.

**Tier 3's silence is an answer, and it is not "pricing…".** Branch 1b used to catch
both "a query is outstanding" and "a query came back with nothing", because the
valuation carried one boolean for both. It rendered ``pricing…`` next to two searches
that had already finished empty, promising a number that was never coming.
:class:`~modules.prices.backend.api.Tier3` now separates them and 1c is the terminal
half.

**Unpriceable is tested next and can never be overridden by a price branch.** Not because it is the
most important state but because it is the only one that is a statement about our
own knowledge rather than about the item. Every other branch presumes a number
exists; putting the "we do not have one" case anywhere but first means some branch
eventually reads ``total_chaos`` on a valuation that has no price, gets ``0.0``, and
quietly writes off a stack of removed items as trash. That is the exact failure
research-notes §7 measured — ~170 ``Veiled Scarab`` — and it is a *silent* failure,
which is why it is structural here rather than a rule someone has to remember.

**The threshold is compared against the stack total, not the unit price.** The
question is "is this worth a stash trip", and ``Jeweller's Orb x2615`` is worth one
whatever a single orb costs.

**The gate outranks the check floor but not the keep threshold.** A gate hit means
"a trade query would settle this", which is exactly ``check``. It cannot promote an
item to ``keep``, because ``keep`` is a claim about value and the gate does not know
any values — the whole point of tier 2 is that it is the thing you consult *because*
you have no price.
"""

from __future__ import annotations

from modules.appraisal.backend.api import (
    BagAppraisal,
    GateResult,
    ItemVerdict,
    Slot,
    Strictness,
    Verdict,
    indexable,
    not_loot,
)
from modules.poeapi.backend.api import NormalizedItem
from modules.prices.backend.api import BagValuation, PriceSource, Valuation

__all__ = ["appraise_bag", "appraise_one", "classify"]


def classify(
    item: NormalizedItem,
    valuation: Valuation,
    gate: GateResult,
    *,
    keep_chaos: float,
    check_chaos: float,
) -> tuple[Verdict, str]:
    """The branches above, plus the sentence that explains the answer."""
    if not_loot(item):
        return Verdict.NOT_LOOT, _not_loot_reason(item)

    if valuation.unpriceable:
        if indexable(item):
            return Verdict.UNPRICEABLE, _unpriceable_reason(valuation)
        # A rare ring has no bulk price and never will. That is not a hole in the
        # index, it is the case tier 2 exists for.
        if valuation.pricing:
            # A tier-3 query is out and has not answered. SPEC §5.4 gives `check`
            # exactly this second job — "or tier-3 pending" — and the word matters:
            # "pricing…" says the number is coming, where the gate summary alone
            # implies we are never going to have one.
            return Verdict.CHECK, _with_gate("pricing…", gate)
        if valuation.no_listings:
            # ...and this is the other half of that word. The search ran, broadened,
            # and found nothing comparable in the league. Terminal, and phrased so
            # nobody waits for it: the item is not worthless, it is *uncompared*.
            return Verdict.CHECK, _with_gate("no matching listings", gate)
        if valuation.tier3_failed:
            return Verdict.CHECK, _with_gate(
                valuation.reason or "the trade search could not run", gate
            )
        if gate.passed:
            # The highlighter claims no number and this reason must not imply one.
            # "worth asking about" is the whole of what it said, and the price check
            # is the player's to run — IMPLEMENTATION-PLAN §5b.
            return Verdict.CHECK, f"worth asking about: {gate.summary}"
        return Verdict.TRASH, _gate_miss_reason(gate)

    # The value column already prints the line total, so the reason says the two
    # things the number cannot: what one unit costs, and where the figure came from.
    provenance = _provenance(valuation)
    if valuation.total_chaos >= keep_chaos:
        return Verdict.KEEP, provenance
    if gate.passed:
        return Verdict.CHECK, f"{provenance} · {gate.summary}"
    if valuation.total_chaos >= check_chaos:
        return Verdict.CHECK, provenance
    return Verdict.TRASH, provenance


_SOURCE_WORDS: dict[PriceSource, str] = {
    PriceSource.NOTE: "your own note",
    PriceSource.BULK: "poe.ninja",
    PriceSource.EXCHANGE: "bulk exchange",
    PriceSource.TRADE: "trade search",
}
"""Four sources, four different claims. "poe.ninja" is a whole-market index;
"bulk exchange" is the median of a handful of live offers on a market poe.ninja does
not index at all; "trade search" is one item's own comparables. A player deciding
whether to believe a number needs to know which of those it is, and the CLI has
printed a source column since Phase 3 precisely so it can."""


def _with_gate(state: str, gate: GateResult) -> str:
    return f"{state} · {gate.summary}" if gate.passed else state


_NOT_LOOT_WORDS: dict[str, str] = {
    "quest": "quest item — cannot be traded or vendored",
    "cosmetic": "cosmetic effect — account-bound",
    "hideout": "hideout decoration — not a loot decision",
}


def _not_loot_reason(item: NormalizedItem) -> str:
    """Why this row is outside the loot decision, in the player's terms.

    Never a price sentence. The whole point is that the value question does not
    apply, so a reason that mentions the index or the gate would reintroduce it.
    """
    return _NOT_LOOT_WORDS.get(item.category, "not a loot decision")


def _provenance(valuation: Valuation) -> str:
    source = _SOURCE_WORDS.get(valuation.source, "poe.ninja")
    price = valuation.price
    if price is not None and price.source is PriceSource.TRADE and price.sample_size:
        # A tier-3 median over one listing is one stranger's asking price, and the
        # first live appraisal printed exactly that as `10.0c · trade search` with
        # nothing to say it rested on a single comparable. The sample size is the
        # cheapest possible honesty here, so it is in the sentence rather than in a
        # detail pane nobody opens.
        source = f"{source} ({price.sample_size} listing{'' if price.sample_size == 1 else 's'})"
    if valuation.stack_size > 1 and price is not None:
        return f"{valuation.stack_size} x {price.chaos:g}c · {source}"
    return source


def _unpriceable_reason(valuation: Valuation) -> str:
    """`prices` already says why, in one clause. Repeating "excluded from the total"
    here would be the third place one screen says it — the block heading and the
    total line both carry it."""
    return valuation.reason or "not in the poe.ninja index for this league"


def _gate_miss_reason(gate: GateResult) -> str:
    if not gate.considered:
        return "no bulk price, and nothing the highlighter can read"
    if gate.strictness is Strictness.STRICT:
        return "no bulk price; no hard criterion met"
    return "no bulk price; nothing worth asking about"


def appraise_one(
    item: NormalizedItem,
    valuation: Valuation,
    gate: GateResult,
    *,
    keep_chaos: float,
    check_chaos: float,
) -> ItemVerdict:
    verdict, reason = classify(
        item, valuation, gate, keep_chaos=keep_chaos, check_chaos=check_chaos
    )
    return ItemVerdict(
        uid=item.uid,
        name=item.name or item.base_type,
        base_type=item.base_type,
        category=item.category,
        rarity=item.rarity.value,
        verdict=verdict,
        valuation=valuation,
        gate=gate,
        reason=reason,
        slot=Slot(x=item.grid.x, y=item.grid.y, w=item.grid.w, h=item.grid.h),
    )


def appraise_bag(
    items: list[NormalizedItem],
    valued: BagValuation,
    gates: list[GateResult],
    *,
    keep_chaos: float,
    check_chaos: float,
    strictness: Strictness,
) -> BagAppraisal:
    """Zip the three parallel sequences into one appraisal.

    ``valued.items`` is positionally aligned with ``items`` — ``value_all`` fans a
    deduplicated lookup back out one row per input item, in order — and the caller
    builds ``gates`` the same way. Asserting the lengths rather than trusting them
    keeps a future change to either side from silently pairing the wrong rows, which
    would misprice every item after the first mismatch.
    """
    if not (len(items) == len(valued.items) == len(gates)):
        raise ValueError(
            "appraise_bag needs one valuation and one gate result per item; got "
            f"{len(items)} items, {len(valued.items)} valuations, {len(gates)} gates"
        )
    verdicts = [
        appraise_one(item, valuation, gate, keep_chaos=keep_chaos, check_chaos=check_chaos)
        for item, valuation, gate in zip(items, valued.items, gates, strict=True)
    ]
    return BagAppraisal(
        verdicts,
        league=valued.league,
        league_source=valued.league_source,
        threshold_chaos=keep_chaos,
        strictness=strictness,
        divine_rate=valued.divine_rate,
        table=valued.table,
        lookups=valued.lookups,
        trade_requests=valued.trade_requests,
    )
