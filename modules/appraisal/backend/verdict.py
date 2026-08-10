"""Prices plus the gate → one of four verdicts (SPEC §5.4).

Pure functions over a :class:`~modules.prices.backend.api.Valuation` and a
:class:`~modules.appraisal.backend.api.GateResult`. Nothing here can perform I/O or
compute a price; if a number is needed it was already asked for.

## The order, and why it is this order

::

    1.  no price, and the bulk index should have carried it   → unpriceable
    2.  stack total at or above the keep threshold            → keep
    3.  the tier-2 gate found something                       → check
    4.  stack total at or above the check floor               → check
    5.  otherwise                                             → trash

**Unpriceable is tested first and can never be overridden.** Not because it is the
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
    Strictness,
    Verdict,
    indexable,
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
    """The five branches above, plus the sentence that explains the answer."""
    if valuation.unpriceable:
        if indexable(item):
            return Verdict.UNPRICEABLE, _unpriceable_reason(valuation)
        # A rare ring has no bulk price and never will. That is not a hole in the
        # index, it is the case tier 2 exists for.
        if gate.passed:
            return Verdict.CHECK, gate.summary
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


def _provenance(valuation: Valuation) -> str:
    if valuation.source is PriceSource.NOTE:
        source = "your own note"
    elif valuation.source is PriceSource.TRADE:
        source = "trade query"
    else:
        source = "poe.ninja"
    if valuation.stack_size > 1 and valuation.price is not None:
        return f"{valuation.stack_size} x {valuation.price.chaos:g}c · {source}"
    return source


def _unpriceable_reason(valuation: Valuation) -> str:
    """`prices` already says why, in one clause. Repeating "excluded from the total"
    here would be the third place one screen says it — the block heading and the
    total line both carry it."""
    return valuation.reason or "not in the poe.ninja index for this league"


def _gate_miss_reason(gate: GateResult) -> str:
    if not gate.considered:
        return "no bulk price, and nothing the gate can read"
    if gate.strictness is Strictness.STRICT:
        return "no bulk price; no hard requirement met"
    return "no bulk price; the gate found nothing"


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
