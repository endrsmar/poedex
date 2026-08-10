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

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from modules.poeapi.backend.api import NormalizedItem, Rarity
from modules.prices.backend.api import (
    LeagueSource,
    ModFocus,
    TableStatus,
    Valuation,
)
from runtime.errors import PoedexError

__all__ = [
    "APPRAISAL_COMPLETE",
    "DEFAULT_CHECK_CHAOS",
    "DEFAULT_KEEP_CHAOS",
    "NOT_LOOT_CATEGORIES",
    "AppraisalApi",
    "AppraisalError",
    "BagAppraisal",
    "GateResult",
    "GateSignal",
    "ItemVerdict",
    "Slot",
    "Strictness",
    "Verdict",
    "indexable",
    "not_loot",
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
    """SPEC §5.4's four states, plus one the first live appraisal proved was missing.

    The first four are all answers to *what should I do with this loot?* The fifth
    exists because some rows in a real backpack are not loot, and every one of the
    four would be a lie about them.
    """

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

    NOT_LOOT = "not_loot"
    """Not a loot decision at all: a quest item, an MTX effect, a hideout decoration.

    The first live appraisal put ``The Mortinomicon Exitio Immortalis`` — a quest
    item — under ``TRASH``, whose headline is *vendor*. A quest item cannot be
    traded and cannot be vendored, so that is not an unhelpful suggestion, it is an
    impossible instruction, and a player who tries to follow it learns the tool does
    not know what it is looking at.

    This is a **fifth verdict** rather than a lane inside an existing one, which
    CLAUDE.md is right to be suspicious of. The bar it clears is that the difference
    is not about layout: `check`'s two lanes ask the player for the same action and
    differ only in what the screen has room to show, while this row is asking for no
    action at all. It also has to survive the bag *grid*, where every cell needs a
    verdict — an item excluded from the verdict list would silently vanish from a map
    whose whole job is to be complete.
    """


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


NOT_LOOT_CATEGORIES: frozenset[str] = frozenset({"quest", "cosmetic", "hideout"})
"""Categories where "what should I do with this?" has no answer worth printing.

* ``quest`` — quest items. Cannot be traded (the trade API has no such name, which is
  why tier 3 finds nothing), cannot be vendored, cannot even be dropped in most
  cases. ``The Mortinomicon Exitio Immortalis`` is the live example.
* ``cosmetic`` — microtransaction effects, from ``2DItems/MicrotransactionItemEffects``.
  Account-bound by construction.
* ``hideout`` — hideout decorations. Some are tradeable, most arrive from MTX and are
  not, and none of them is a stash-trip decision.

Deliberately **not** here: ``prophecy``. Prophecies were genuinely tradeable items
when the mechanic existed, so calling one "not loot" would be a different wrong
answer; it cannot appear in a live bag anyway.

Kept as an explicit set rather than derived from "nothing could price it", because
that description also fits a rare — and a rare is the one thing this tool exists to
have an opinion about."""


def not_loot(item: NormalizedItem) -> bool:
    """Is this row outside the loot decision entirely?

    Checked before every pricing question, because it makes all of them moot: an item
    that cannot be sold has no market price to be missing, so ``unpriceable`` would
    be as wrong as ``trash``.

    Rarity is checked as well as category because GGG's own data says it twice —
    ``frameType 7`` sets both — and either one arriving alone should still be caught.
    """
    return item.rarity is Rarity.QUEST or item.category in NOT_LOOT_CATEGORIES


def _widen(value: float, widen: float) -> float:
    """A measured roll, dropped ``widen`` below itself. ``103 → 82``.

    Mirrors :func:`modules.prices.backend.trade.widened` rather than importing it:
    `appraisal` may import `prices`' **api** and nothing else, and a four-line
    arithmetic helper is not worth widening that surface for.
    """
    floor = value * (1.0 - widen)
    return float(math.floor(floor)) if floor >= 1 else round(floor, 2)


class GateSignal:
    """One reason the tier-2 gate had an opinion about an item.

    Kept as an object rather than a string so a surface can show *why* an item is
    worth checking without re-deriving it, and so the strict and generous gates can
    be compared signal by signal in a test.

    It also carries the **mod lines** behind the opinion, which is what lets tier 3
    ask a question shaped like the reason we escalated. Before that, the trade query
    ANDed every mod on the item and a six-mod rare matched nothing at all.
    """

    __slots__ = ("detail", "hard", "label", "mods", "name", "value")

    def __init__(
        self,
        name: str,
        detail: str,
        *,
        hard: bool = False,
        mods: Sequence[str] = (),
        value: float | None = None,
        label: str = "",
    ) -> None:
        self.name = name
        self.detail = detail
        self.hard = hard
        """A SPEC §5.2 hard requirement — the ones the strict gate accepts. Soft
        signals are the generous gate's additions."""

        self.mods = tuple(mods)
        """The item's own mod lines that produced this signal. Empty for signals that
        are not about mods at all — influence, six-link, the base allowlist."""

        self.value = value
        """The roll the gate measured, when it measured one. ``None`` means the gate
        observed the mod group was *present* and made no claim about how well it
        rolled — so nothing downstream may invent a roll floor from it."""

        self.label = label or name.replace("_", " ")

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "detail": self.detail,
            "hard": self.hard,
            "mods": list(self.mods),
            "value": self.value,
        }

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

    def focus(self, *, limit: int = 2, widen: float = 0.2) -> list[ModFocus]:
        """The mods a tier-3 query should filter on, in the gate's own priority.

        This is the join between tier 2 and tier 3, and it is the fix for the first
        live appraisal's central failure: the trade query used to AND *every*
        resolvable mod on the item, so a six-mod rare became a near-exact-match search
        that two of three flagged items matched zero listings with.

        The gate has already decided which mods make this item interesting. Asking the
        market about *those* keeps the query aligned with the reason we escalated, and
        keeps it a superset rather than a fingerprint.

        Two rules, both of them about not claiming more than the gate did:

        * A signal with a measured roll becomes a ``min`` filter **widened by
          ``widen``** — never the exact roll, which matches almost nothing. The
          trade site's own filters are ranges for this reason.
        * A signal that only observed the group was *present* becomes a presence
          filter with no floor, because that is all the gate actually claimed.

        Hard signals that are not about mods (influence, six-link, ilvl 86, the base
        allowlist) contribute nothing here. They are real reasons to look at an item
        and they are *not* stat filters; folding them in would narrow the query on
        axes the median is not being taken over. The cost is that a fractured or
        influenced item is priced against its plain equivalents, which errs low — and
        low with a number still lands in ``check``, where the player looks.
        """
        picked: list[ModFocus] = []
        seen: set[str] = set()
        for signal in self.signals:
            for text in signal.mods:
                if text in seen:
                    continue
                seen.add(text)
                # A summed group (resistances across four lines) has a roll but no
                # single line that carries it, so no line gets a floor from it.
                minimum = (
                    _widen(signal.value, widen)
                    if signal.value is not None and len(signal.mods) == 1
                    else None
                )
                picked.append(ModFocus(text=text, minimum=minimum, label=signal.label))
                if len(picked) >= limit:
                    return picked
        return picked

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "considered": self.considered,
            "strictness": self.strictness.value,
            "signals": [signal.to_json() for signal in self.signals],
        }

    def __repr__(self) -> str:
        return f"GateResult({self.strictness.value}, {[s.name for s in self.signals]})"


@dataclass(frozen=True, slots=True)
class Slot:
    """Where the item sits in the container it came from.

    Carried on the verdict because SPEC §6.3 makes the bag grid a *map*: a green
    cell is the exact slot the player's cursor has to find, which is the one thing a
    verdict list cannot express. It is `poeapi`'s own `Grid`, flattened to four ints
    rather than re-exported, so `appraisal`'s public surface does not oblige every
    consumer to import `poeapi`'s models to read a coordinate.
    """

    x: int = 0
    y: int = 0
    w: int = 1
    h: int = 1

    def to_json(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


class ItemVerdict:
    """One item's verdict, the valuation behind it, and why."""

    __slots__ = (
        "base_type",
        "category",
        "gate",
        "name",
        "rarity",
        "reason",
        "slot",
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
        slot: Slot | None = None,
    ) -> None:
        self.slot = slot
        """``None`` when the caller had no placement to give — an equipment slot, a
        synthetic row in a test. A surface draws a grid only from the rows that have
        one, and lists the rest."""

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
        """Would a tier-3 query be worth a request on this item?

        For a bag, ``appraisal`` acts on this directly (module docstring); for a
        stash it stays a suggestion, which is what SPEC §5.3 protects.
        """
        return self.gate.passed

    @property
    def pricing(self) -> bool:
        """A tier-3 query for this item was started and has not answered."""
        return self.valuation.pricing

    @property
    def no_listings(self) -> bool:
        """Tier 3 ran, broadened, and still matched nothing. A terminal answer."""
        return self.valuation.no_listings

    @property
    def tier3_failed(self) -> bool:
        """Tier 3 could not run at all. Also terminal, for a different reason."""
        return self.valuation.tier3_failed

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
            "slot": self.slot.to_json() if self.slot else None,
            "verdict": self.verdict.value,
            "stack_size": self.stack_size,
            "total_chaos": round(self.total_chaos, 4),
            "unpriceable": self.unpriceable,
            "pricing": self.pricing,
            "no_listings": self.no_listings,
            "tier3": self.valuation.tier3.value,
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
        "league_source",
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
        league_source: LeagueSource | None = None,
        divine_rate: float | None = None,
        table: TableStatus | None = None,
        lookups: int = 0,
        trade_requests: int = 0,
    ) -> None:
        self.items = list(items)
        self.league = league
        self.league_source = league_source
        """Why :attr:`league` is what it is, carried through from the valuation. A
        verdict screen that shows a bare league name asks the player to trust it;
        one that shows where the name came from lets them check it."""

        self.threshold_chaos = threshold_chaos
        self.strictness = strictness
        self.divine_rate = divine_rate
        self.table = table
        self.lookups = lookups
        self.trade_requests = trade_requests
        """Trade-API requests this pass made. Zero at ``strict``, always — a stash is
        never escalated eagerly (SPEC §5.3), and this field exists so a test can
        prove it rather than a comment claiming it."""

    def of(self, verdict: Verdict) -> list[ItemVerdict]:
        return [item for item in self.items if item.verdict is verdict]

    @property
    def counts(self) -> dict[str, int]:
        """Every state, always present, zeroes included. A missing key is how a
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
        """What a tier-3 pass would cost, in items. Two requests each."""
        return [item for item in self.items if item.escalate]

    @property
    def pricing(self) -> list[ItemVerdict]:
        """Rows whose tier-3 query was started and has not answered.

        While this is non-empty :attr:`total_chaos` is a **floor**: every row in it
        is worth an unknown amount that is almost certainly not zero. SPEC §5.3 asks
        a surface to say ``≥ N div`` rather than ``N div``, and this is the flag that
        makes that possible without the total lying in the meantime.
        """
        return [item for item in self.items if item.pricing]

    @property
    def no_listings(self) -> list[ItemVerdict]:
        """Rows whose tier-3 search finished and matched nothing.

        Kept apart from :attr:`pricing` because the footnote under the bag total used
        to describe these as "still pricing", which is the same lie the row-level
        ``pricing…`` told: it says a number is coming when the search already came
        back empty. Their value is unknown and excluded from the total, exactly like
        an unescalated gate hit — which is why they do not make the total a *floor*
        in the ``≥`` sense, since nothing about them is going to resolve.
        """
        return [item for item in self.items if item.no_listings]

    @property
    def total_is_floor(self) -> bool:
        """``≥`` means "a number is outstanding and will move this figure". Only
        :attr:`pricing` qualifies; a finished-and-empty search moves nothing."""
        return bool(self.pricing)

    def ranked(self) -> list[ItemVerdict]:
        """Interesting first: keep, check, unpriceable, trash, not-loot.

        Unpriceable sits above trash rather than below it because an unknown is a
        thing to look at and a trash verdict is a thing to stop looking at. Not-loot
        sits below trash because it is the only block that asks for nothing at all.

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
            Verdict.NOT_LOOT: 4,
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
            "league_source": self.league_source.value if self.league_source else None,
            "league_overridden": (
                self.league_source is not LeagueSource.CHARACTER
                if self.league_source
                else None
            ),
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
            "pricing_count": len(self.pricing),
            "no_listings_count": len(self.no_listings),
            "total_is_floor": self.total_is_floor,
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
        league: str | None = None,
        override: str | None = None,
        escalate: bool | None = None,
    ) -> BagAppraisal:
        """Verdict every item. ``None`` means "use the configured value".

        ``escalate`` asks for (or refuses) eager tier-3 pricing of the gate's hits.
        It is honoured only at ``generous`` strictness: a stash is never escalated
        eagerly whatever the caller says, because the reason SPEC §5.3 forbids it —
        hundreds of candidates against a five-per-ten-seconds budget — is a property
        of the stash, not of the caller's intent.

        ``league`` is the league the items are in — ``ItemSet.league`` — and
        ``override`` deliberately prices them against a different one. Both go
        straight to ``PricesApi``, which refuses to price anything it cannot place in
        an economy; nothing here invents one.
        """
        ...

    async def appraise_item(
        self,
        item: NormalizedItem,
        *,
        strictness: Strictness | None = None,
        league: str | None = None,
        override: str | None = None,
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
