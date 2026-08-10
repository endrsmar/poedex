"""The public surface of the `appraisal` module.

**This is the only file in this module that other modules may import** (plan §1.4,
enforced by ``tests/test_boundaries.py``).

Four decisions are encoded in these types rather than left to a caller.

* **``unpriceable`` is one of the four verdicts, not a flavour of ``trash``.**
  :class:`Verdict` has four members and :class:`BagAppraisal` reports the
  unpriceable rows, their unit count and their exclusion from the total as separate
  numbers. The live account holds ~170 of a removed item that poe.ninja's league
  index does not carry (research-notes §7); calling those ``trash`` — or, worse,
  summing them as zero — understates the bag and destroys trust in the total. There
  is deliberately no ``BagAppraisal.total_including_unpriceable``, because a number
  that pretends to know what a hole is worth is the bug this whole model exists to
  prevent.

* **"Not in the bulk index" and "bulk was never going to price this" are different
  facts.** `prices` cannot tell them apart — both come back as
  :attr:`~modules.prices.backend.api.Valuation.unpriceable` — but appraisal can, and
  must, because they want opposite treatment. A ``Veiled Scarab`` missing from the
  index is a hole in the total. A rare ring missing from the index is *normal*: no
  bulk table has ever priced rares, and that is exactly what the tier-2 gate and
  tier 3 exist for. :func:`indexable` draws the line, and the reason lives with the
  verdict so the distinction is inspectable rather than implied.

* **The keep threshold is a setting, never a literal.** :data:`DEFAULT_KEEP_CHAOS`
  is the module's default and the only place the number 20 appears. SPEC §11 lists
  the default as unresolved — ~20c gives a busy panel, divine-tier a quiet one — and
  Phase 4's own exit criterion is that appraising a real bag is what settles it.

* **A verdict pass makes no requests.** :meth:`AppraisalApi.appraise` is built on
  ``PricesApi.value_all``, which reads prefetched tables, and the tier-2 gate is pure
  local arithmetic. :attr:`BagAppraisal.trade_requests` is carried so a test can
  assert the zero rather than a comment claiming it.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from modules.poeapi.backend.api import NormalizedItem, Rarity
from modules.prices.backend.api import TableStatus, Valuation
from runtime.errors import PoedexError

__all__ = [
    "APPRAISAL_COMPLETE",
    "DEFAULT_CHECK_CHAOS",
    "DEFAULT_KEEP_CHAOS",
    "AppraisalApi",
    "AppraisalError",
    "BagAppraisal",
    "GateResult",
    "GateSignal",
    "ItemVerdict",
    "Strictness",
    "Verdict",
    "indexable",
]

APPRAISAL_COMPLETE = "appraisal_complete"
"""Event topic emitted after a bag is appraised. Payload carries the counts, the
total and the threshold that produced them — enough for a surface to redraw without
asking for the whole bag again."""

DEFAULT_KEEP_CHAOS = 20.0
"""SPEC §11's open question, given a default and a name. The only literal 20 in the
module: everything else reads the ``keep_threshold_chaos`` setting."""

DEFAULT_CHECK_CHAOS = 1.0
"""Below the keep threshold but not worth a trip on its own. SPEC §5.4 calls this
"below threshold but non-trivial"; one chaos is the smallest amount a player will
pick up off the floor for."""


class AppraisalError(PoedexError):
    """A problem producing a verdict."""


class Strictness(StrEnum):
    """How readily the tier-2 gate says "look at this" (SPEC §5.2).

    Same code, opposite biases, because the two contexts have opposite failure
    costs.
    """

    GENEROUS = "generous"
    """The bag. A false negative tells the player to vendor something good, and the
    only cost of a false positive is one trade query they can decline."""

    STRICT = "strict"
    """The stash. The item already survived bag triage and is sitting in storage,
    not about to be vendored, so a false negative costs nothing today — while a
    generous gate at 818-item scale produces hundreds of false positives and the
    digest becomes noise."""


class Verdict(StrEnum):
    """SPEC §5.4. Four states, and the fourth is not a worse third."""

    KEEP = "keep"
    """At or above the keep threshold."""

    CHECK = "check"
    """Below the threshold but non-trivial, or the tier-2 gate flagged it and a
    tier-3 query would settle it."""

    TRASH = "trash"
    """Confidently below the threshold. For a rare, that means bulk could not price
    it *and* the gate found nothing — which is the answer the gate exists to give."""

    UNPRICEABLE = "unpriceable"
    """The bulk index should carry this and does not. **Not** zero, and not trash."""


# Categories the poe.ninja overviews are supposed to cover (SPEC §5.1). An item in
# one of these with no price is a hole in the index; an item outside them with no
# price is simply an item bulk pricing was never going to reach.
_INDEXED_CATEGORIES: frozenset[str] = frozenset(
    {"currency", "fragment", "card", "map", "gem"}
)

# ...and rarities that are indexed whatever the category says, because poe.ninja
# lists uniques and relics by name across every equipment slot.
_INDEXED_RARITIES: frozenset[Rarity] = frozenset({Rarity.UNIQUE, Rarity.RELIC})


def indexable(item: NormalizedItem) -> bool:
    """Should the bulk index carry this item?

    The dividing line between :attr:`Verdict.UNPRICEABLE` and "this is a rare, of
    course bulk has no price for it". Getting it wrong in one direction hides a hole
    in the total; getting it wrong in the other floods the panel with question marks
    for every white sceptre in the bag.

    Deliberately conservative: an unrecognised category counts as *not* indexed, so a
    new item class GGG invents shows up as a gate decision rather than as a claimed
    gap in poe.ninja's coverage.
    """
    if item.rarity in _INDEXED_RARITIES:
        return True
    if item.rarity in (Rarity.RARE, Rarity.MAGIC):
        # A magic or rare *map* is still a map and still priced by tier.
        return item.category == "map"
    return item.category in _INDEXED_CATEGORIES


class GateSignal:
    """One reason the tier-2 gate had an opinion about an item.

    Kept as an object rather than a string so a surface can show *why* an item is
    worth checking without re-deriving it, and so the strict and generous gates can
    be compared signal by signal in a test.
    """

    __slots__ = ("detail", "hard", "name")

    def __init__(self, name: str, detail: str, *, hard: bool = False) -> None:
        self.name = name
        self.detail = detail
        self.hard = hard
        """A SPEC §5.2 hard requirement — the ones the strict gate accepts. Soft
        signals are the generous gate's additions."""

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "detail": self.detail, "hard": self.hard}

    def __repr__(self) -> str:
        return f"GateSignal({self.name!r}, hard={self.hard})"


class GateResult:
    """What the tier-2 gate decided, and on what evidence."""

    __slots__ = ("considered", "signals", "strictness")

    def __init__(
        self,
        signals: Sequence[GateSignal] = (),
        *,
        strictness: Strictness = Strictness.GENEROUS,
        considered: bool = True,
    ) -> None:
        self.signals = list(signals)
        self.strictness = strictness
        self.considered = considered
        """``False`` when the item never entered the gate at all — currency, cards,
        a bulk tab. SPEC §5.2: bulk tabs never enter tier 2 or 3."""

    @property
    def passed(self) -> bool:
        return bool(self.signals)

    @property
    def summary(self) -> str:
        return ", ".join(signal.detail for signal in self.signals)

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "considered": self.considered,
            "strictness": self.strictness.value,
            "signals": [signal.to_json() for signal in self.signals],
        }

    def __repr__(self) -> str:
        return f"GateResult({self.strictness.value}, {[s.name for s in self.signals]})"


class ItemVerdict:
    """One item's verdict, the valuation behind it, and why."""

    __slots__ = (
        "base_type",
        "category",
        "gate",
        "name",
        "rarity",
        "reason",
        "uid",
        "valuation",
        "verdict",
    )

    def __init__(
        self,
        *,
        uid: str,
        name: str,
        base_type: str,
        category: str,
        rarity: str,
        verdict: Verdict,
        valuation: Valuation,
        gate: GateResult,
        reason: str,
    ) -> None:
        self.uid = uid
        self.name = name
        self.base_type = base_type
        self.category = category
        self.rarity = rarity
        self.verdict = verdict
        self.valuation = valuation
        self.gate = gate
        self.reason = reason
        """One line a surface can show verbatim. Never "trash": *why* trash."""

    @property
    def stack_size(self) -> int:
        return self.valuation.stack_size

    @property
    def total_chaos(self) -> float:
        """Stack-aware chaos. ``0.0`` for an unpriceable row — read
        :attr:`unpriceable` before adding this to anything."""
        return self.valuation.total_chaos

    @property
    def unpriceable(self) -> bool:
        return self.verdict is Verdict.UNPRICEABLE

    @property
    def escalate(self) -> bool:
        """Would a tier-3 query be worth one request on this item? On demand only —
        nothing in this module acts on it (SPEC §5.3)."""
        return self.gate.passed

    @property
    def hard_signals(self) -> int:
        """How many of SPEC §5.2's hard requirements this item hit. Used to rank a
        ``check`` block, where every row has the same value — namely none."""
        return sum(1 for signal in self.gate.signals if signal.hard)

    def to_json(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "base_type": self.base_type,
            "category": self.category,
            "rarity": self.rarity,
            "verdict": self.verdict.value,
            "stack_size": self.stack_size,
            "total_chaos": round(self.total_chaos, 4),
            "unpriceable": self.unpriceable,
            "escalate": self.escalate,
            "reason": self.reason,
            "gate": self.gate.to_json(),
            "valuation": self.valuation.to_json(),
        }

    def __repr__(self) -> str:
        return f"ItemVerdict({self.name!r}, {self.verdict.value})"


class BagAppraisal:
    """Every verdict, plus the numbers a surface shows without recomputing them."""

    __slots__ = (
        "divine_rate",
        "items",
        "league",
        "lookups",
        "strictness",
        "table",
        "threshold_chaos",
        "trade_requests",
    )

    def __init__(
        self,
        items: Sequence[ItemVerdict],
        *,
        league: str,
        threshold_chaos: float,
        strictness: Strictness,
        divine_rate: float | None = None,
        table: TableStatus | None = None,
        lookups: int = 0,
        trade_requests: int = 0,
    ) -> None:
        self.items = list(items)
        self.league = league
        self.threshold_chaos = threshold_chaos
        self.strictness = strictness
        self.divine_rate = divine_rate
        self.table = table
        self.lookups = lookups
        self.trade_requests = trade_requests
        """Trade-API requests this pass made. **Always zero** — tier 3 is on demand
        only (SPEC §5.3), and this field exists so a test can prove it."""

    def of(self, verdict: Verdict) -> list[ItemVerdict]:
        return [item for item in self.items if item.verdict is verdict]

    @property
    def counts(self) -> dict[str, int]:
        """All four states, always present, zeroes included. A missing key is how a
        tally silently loses a state."""
        return {v.value: len(self.of(v)) for v in Verdict}

    @property
    def total_chaos(self) -> float:
        """Chaos across everything that has a price. Unpriceable rows contribute
        nothing *and are not counted as nothing* — see :attr:`unpriceable_stack`."""
        return sum(item.total_chaos for item in self.items if not item.unpriceable)

    @property
    def total_divine(self) -> float | None:
        if not self.divine_rate:
            return None
        return self.total_chaos / self.divine_rate

    @property
    def unpriceable_stack(self) -> int:
        """Units, not rows. One row of 170 removed scarabs is a 170-unit hole."""
        return sum(item.stack_size for item in self.of(Verdict.UNPRICEABLE))

    @property
    def escalation_candidates(self) -> list[ItemVerdict]:
        """What a tier-3 pass would cost, in items. One request each, on demand."""
        return [item for item in self.items if item.escalate]

    def ranked(self) -> list[ItemVerdict]:
        """Interesting first: keep, check, unpriceable, trash.

        Unpriceable sits above trash rather than below it because an unknown is a
        thing to look at and a trash verdict is a thing to stop looking at.

        Within a block: gate hits first, then the count of *hard* signals, then value
        descending, then stack size, then name. The hard-signal tie-break exists
        because a ``check`` block is mostly rows worth ``0c`` — a six-linked
        influenced rare and a rare with three mediocre mods sort identically on value
        and are nothing alike, and value-only ordering would bury the first under the
        second. Name last, so two runs of the same bag never disagree.
        """
        rank = {
            Verdict.KEEP: 0,
            Verdict.CHECK: 1,
            Verdict.UNPRICEABLE: 2,
            Verdict.TRASH: 3,
        }
        return sorted(
            self.items,
            key=lambda item: (
                rank[item.verdict],
                not item.escalate,
                -item.hard_signals,
                -item.total_chaos,
                -item.stack_size,
                item.name,
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "league": self.league,
            "strictness": self.strictness.value,
            "threshold_chaos": self.threshold_chaos,
            "items": [item.to_json() for item in self.ranked()],
            "counts": self.counts,
            "total_chaos": round(self.total_chaos, 4),
            "total_divine": (
                round(self.total_divine, 4) if self.total_divine is not None else None
            ),
            "divine_rate": self.divine_rate,
            "unpriceable_count": len(self.of(Verdict.UNPRICEABLE)),
            "unpriceable_stack": self.unpriceable_stack,
            "escalation_candidates": len(self.escalation_candidates),
            "lookups": self.lookups,
            "trade_requests": self.trade_requests,
            "table": self.table.to_json() if self.table else None,
        }

    def __repr__(self) -> str:
        counts = " ".join(f"{k}={v}" for k, v in self.counts.items())
        return f"BagAppraisal({counts}, {self.total_chaos:.0f}c)"


@runtime_checkable
class AppraisalApi(Protocol):
    """What dependents get from ``ctx.require(AppraisalApi)``.

    Every method here is guaranteed **not** to touch the network beyond what
    ``PricesApi`` already promises: prices come from prefetched bulk tables and the
    gate is local. That is what lets a bag be appraised on every zone transition.
    """

    async def appraise(
        self,
        items: Sequence[NormalizedItem],
        *,
        strictness: Strictness | None = None,
        threshold_chaos: float | None = None,
    ) -> BagAppraisal:
        """Verdict every item. ``None`` means "use the configured value"."""
        ...

    async def appraise_item(
        self, item: NormalizedItem, *, strictness: Strictness | None = None
    ) -> ItemVerdict:
        """One item, same rules."""
        ...

    def gate(
        self, item: NormalizedItem, *, strictness: Strictness = Strictness.GENEROUS
    ) -> GateResult:
        """Tier 2 alone, with no pricing. Synchronous because it is arithmetic."""
        ...

    def threshold(self) -> float:
        """The configured keep threshold in chaos."""
        ...
