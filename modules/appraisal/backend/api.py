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
  ``PricesApi.value_all``, which reads prefetched tables, and the tier-2 highlighter
  is local. :attr:`BagAppraisal.trade_requests` is carried so a test can assert the
  zero rather than a comment claiming it. Since Phase 9 that zero is **structural**:
  there is no escalation path left for a future change to switch back on.

* **A rare with no manual check has no price, and is never given one.**
  IMPLEMENTATION-PLAN §5b: automatic rare pricing failed twice against a live
  account, once by ANDing every mod and matching zero listings, once by querying one
  loose mod and reporting a stranger's asking price as a median over n=1. The gate
  now *highlights* and claims nothing; :class:`ItemHighlight` is the checkbox list
  the player answers with, and :meth:`AppraisalApi.price_check` runs **their** query.
  :attr:`BagAppraisal.unchecked` is what the total therefore leaves out.
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
    QuerySpec,
    TableStatus,
    TradeQuote,
    Valuation,
)
from runtime.errors import PoedexError

__all__ = [
    "APPRAISAL_COMPLETE",
    "DEFAULT_CHECK_CHAOS",
    "DEFAULT_KEEP_CHAOS",
    "DEFAULT_WIDEN",
    "NOT_LOOT_CATEGORIES",
    "AppraisalApi",
    "AppraisalError",
    "BagAppraisal",
    "GateResult",
    "GateSignal",
    "ItemHighlight",
    "ItemVerdict",
    "ModOption",
    "PriceCheck",
    "Selection",
    "Slot",
    "Strictness",
    "Verdict",
    "indexable",
    "not_loot",
    "widened_floor",
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


DEFAULT_WIDEN = 0.2
"""How far below a ticked roll the ``min`` filter sits — 20%.

An exact-value filter on a random roll matches almost nothing, and exact matching is
what produced the zero-listing searches §5b was written about. The trade site's own
filters are ranges for the same reason. A player who ticks ``+103 to maximum Life``
is asking about items *like this one*, which is the ``+82``-and-up band, not the
``+103``-exactly one.
"""


def widened_floor(value: float, widen: float = DEFAULT_WIDEN) -> float:
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

    @property
    def flagged_mods(self) -> tuple[str, ...]:
        """The mod lines the highlighter reacted to, deduplicated, in its own order.

        **Not a query.** ``GateResult.focus()`` used to live here and hand exactly
        this down to `prices` as a filter set, and IMPLEMENTATION-PLAN §5b deleted the
        idea rather than the parameters: which mods make *this* item interesting is
        player knowledge, and a proposal that builds its own query is a decision
        wearing a proposal's clothes. What this is for is the **pre-tick** — the
        checkbox list starts with these ticked and the player disagrees with a press.
        """
        seen: dict[str, None] = {}
        for signal in self.signals:
            for text in signal.mods:
                seen.setdefault(text, None)
        return tuple(seen)

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "considered": self.considered,
            "strictness": self.strictness.value,
            "signals": [signal.to_json() for signal in self.signals],
        }

    def __repr__(self) -> str:
        return f"GateResult({self.strictness.value}, {[s.name for s in self.signals]})"


TIER_UNKNOWN = "unknown"
"""What a surface shows where attribution is ambiguous. Roughly one affix line in
five, measured on the live-derived fixtures.

`moddb` refuses to name a tier for :data:`Attribution.AMBIGUOUS` and
:data:`Attribution.UNKNOWN`, and this is that refusal reaching the screen intact. A
panel that renders "T2" when the truth is "probably T2, possibly T3" is worse than
one that admits it: the first is a lie the player acts on and cannot check."""


@dataclass(frozen=True, slots=True)
class ModOption:
    """One line of an item as a **tickable** row, with what `moddb` will vouch for.

    The tier fields are deliberately narrow. ``tier`` is populated only where
    attribution is exact, ``tier_label`` is :meth:`ModMatch.describe`'s own words, and
    where the database will not commit the label is :data:`TIER_UNKNOWN` rather than a
    best guess. Nothing here ever shows a tier `moddb` did not assert.
    """

    index: int
    """Position in the item's own line order. The selection is made of these, so a
    tick survives a re-render and cannot be confused by two identical mod texts —
    which happens: two ``+12% to Fire Resistance`` suffixes are a real item."""

    text: str
    origin: str = "explicit"
    affix: str | None = None
    """``prefix`` / ``suffix`` / ``None``. Available more often than a tier is: two
    groups can be indistinguishable and still both be suffixes."""

    tier: int | None = None
    tiers: int | None = None
    tier_label: str = TIER_UNKNOWN
    attribution: str = "unknown"
    top_tier: bool = False
    """The roll is in the best tier its group reaches *on this base*."""

    value: float | None = None
    ceiling: float | None = None
    """The best this mod can roll here — what turns "is 85 life good?" into a question
    with an answer."""

    influences: tuple[str, ...] = ()
    preticked: bool = False
    tradeable: bool = True
    """Whether `moddb`'s **offline** bridge found a trade stat id for this sentence.

    Advisory, and deliberately not a veto. The bridge is a trimmed artifact and it
    has holes — ``98% increased Energy Shield`` is a common mod with no entry — so
    disabling the row would silently narrow what the player is allowed to ask, which
    is the same failure as building the query for them. The live stat document is the
    authority: :func:`modules.prices.backend.trade.build_plan` drops what it cannot
    resolve *and says how many it dropped*, so a filter that goes nowhere is visible
    in the query description rather than invisible in a greyed-out box."""

    @property
    def suggested_minimum(self) -> float | None:
        """The ``min`` a tick on this row should search for. Never the exact roll."""
        return None if self.value is None else widened_floor(self.value)

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "origin": self.origin,
            "affix": self.affix,
            "tier": self.tier,
            "tiers": self.tiers,
            "tier_label": self.tier_label,
            "attribution": self.attribution,
            "top_tier": self.top_tier,
            "value": self.value,
            "ceiling": self.ceiling,
            "influences": list(self.influences),
            "preticked": self.preticked,
            "tradeable": self.tradeable,
            "suggested_minimum": self.suggested_minimum,
        }


class ItemHighlight:
    """Everything the checkbox list needs about one item, and no price at all.

    This is the *proposal*. It says the item is worth asking about and why, offers the
    lines to ask with and pre-ticks the ones `moddb` says are high-tier here, and
    reports how many affix slots are still free — because "at least one open prefix"
    is a real trade filter and a real source of crafting value.

    It deliberately carries no number. The gate claims none, and a highlight that
    arrived with an estimate attached would be the automatic pricing this phase
    deleted, wearing a different name.
    """

    __slots__ = (
        "base_type",
        "counts_are_certain",
        "gate",
        "ilvl",
        "max_prefixes",
        "max_suffixes",
        "mods",
        "name",
        "note",
        "open_prefixes",
        "open_suffixes",
        "rarity",
        "top_affix_level",
        "uid",
    )

    def __init__(
        self,
        *,
        uid: str,
        name: str,
        base_type: str,
        rarity: str,
        ilvl: int,
        gate: GateResult,
        mods: Sequence[ModOption] = (),
        open_prefixes: int = 0,
        open_suffixes: int = 0,
        max_prefixes: int = 3,
        max_suffixes: int = 3,
        counts_are_certain: bool = False,
        top_affix_level: int | None = None,
        note: str = "",
    ) -> None:
        self.uid = uid
        self.name = name
        self.base_type = base_type
        self.rarity = rarity
        self.ilvl = ilvl
        self.gate = gate
        self.mods = list(mods)
        self.open_prefixes = open_prefixes
        self.open_suffixes = open_suffixes
        self.max_prefixes = max_prefixes
        self.max_suffixes = max_suffixes
        self.counts_are_certain = counts_are_certain
        """Whether the open-affix numbers may be *used*. A filter built on "1 open
        prefix" that is actually zero returns nothing and looks like a dead search, so
        the uncertainty has to be visible to whoever builds the filter."""

        self.top_affix_level = top_affix_level
        self.note = note
        """How much of the item the database could read, in one line."""

    @property
    def highlighted(self) -> bool:
        return self.gate.passed

    @property
    def preticked(self) -> tuple[int, ...]:
        return tuple(option.index for option in self.mods if option.preticked)

    def selection(
        self,
        indexes: Sequence[int] | None = None,
        *,
        open_prefixes: int | None = None,
        open_suffixes: int | None = None,
        widen: float = DEFAULT_WIDEN,
    ) -> Selection:
        """Turn ticks into a selection. ``None`` means "the pre-ticked ones"."""
        wanted = self.preticked if indexes is None else tuple(dict.fromkeys(indexes))
        return Selection(
            uid=self.uid,
            mods=wanted,
            open_prefixes=open_prefixes,
            open_suffixes=open_suffixes,
            widen=widen,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "base_type": self.base_type,
            "rarity": self.rarity,
            "ilvl": self.ilvl,
            "highlighted": self.highlighted,
            "gate": self.gate.to_json(),
            "mods": [option.to_json() for option in self.mods],
            "preticked": list(self.preticked),
            "open_prefixes": self.open_prefixes,
            "open_suffixes": self.open_suffixes,
            "max_prefixes": self.max_prefixes,
            "max_suffixes": self.max_suffixes,
            "counts_are_certain": self.counts_are_certain,
            "top_affix_level": self.top_affix_level,
            "note": self.note,
        }

    def __repr__(self) -> str:
        return f"ItemHighlight({self.name!r}, {len(self.mods)} mods, {self.highlighted})"


@dataclass(frozen=True, slots=True)
class Selection:
    """What the player ticked. The **only** input the query is built from.

    ``mods`` are :attr:`ModOption.index` values, not texts, so two identical mod lines
    on one item stay two rows and a tick lands on the one that was pressed.
    """

    uid: str
    mods: tuple[int, ...] = ()
    open_prefixes: int | None = None
    open_suffixes: int | None = None
    widen: float = DEFAULT_WIDEN

    @property
    def asks_anything(self) -> bool:
        return bool(self.mods) or self.open_prefixes is not None or self.open_suffixes is not None

    def spec(self, highlight: ItemHighlight) -> QuerySpec:
        """Build the trade query from the ticks, and from nothing else.

        Three rules, each of them a fix for something that went wrong live:

        * A ticked roll becomes ``min = roll * (1 - widen)``, never the exact value.
          Exact matching is what returned zero listings on two of three rares.
        * A ticked line with no number becomes a presence filter, because that is all
          there is to claim about it.
        * A line the **live** stat index cannot resolve is dropped downstream, by
          ``build_plan``, which counts what it dropped into the query description.
          It is not filtered out here: `moddb`'s offline bridge has holes, and a
          selection quietly shrunk before it is sent is a query nobody can check.
        """
        by_index = {option.index: option for option in highlight.mods}
        focus: list[ModFocus] = []
        for index in self.mods:
            option = by_index.get(index)
            if option is None:
                continue
            minimum = (
                widened_floor(option.value, self.widen) if option.value is not None else None
            )
            focus.append(
                ModFocus(
                    text=option.text,
                    minimum=minimum,
                    label=option.text,
                    origin=option.origin,
                )
            )
        return QuerySpec(
            mods=tuple(focus),
            open_prefixes=self.open_prefixes,
            open_suffixes=self.open_suffixes,
            broaden=False,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "mods": list(self.mods),
            "open_prefixes": self.open_prefixes,
            "open_suffixes": self.open_suffixes,
            "widen": self.widen,
        }


class PriceCheck:
    """The answer to a manual check: a number, or an honest reason there is none."""

    __slots__ = ("chaos", "divine_rate", "highlight", "league", "quote", "selection", "spent")

    def __init__(
        self,
        *,
        highlight: ItemHighlight,
        selection: Selection,
        league: str,
        quote: TradeQuote | None = None,
        spent: int = 0,
        divine_rate: float | None = None,
    ) -> None:
        self.highlight = highlight
        self.selection = selection
        self.league = league
        self.quote = quote
        self.spent = spent
        """Trade requests this check cost. Two normally — one search, one fetch."""

        self.divine_rate = divine_rate
        self.chaos = quote.chaos if quote is not None else None

    @property
    def priced(self) -> bool:
        return self.chaos is not None

    @property
    def comparables(self) -> int:
        """How many listings the whole search matched, before any fetch.

        Read this before believing the number. A ``total`` of 1 makes "median of the
        cheapest N" one stranger's asking price wearing a median's clothes, which is
        exactly what reported 10c for a 1c jewel.
        """
        return self.quote.total if self.quote is not None else 0

    @property
    def thin(self) -> bool:
        """Fewer comparables than a median means anything over."""
        return self.priced and self.comparables < 5

    @property
    def reason(self) -> str:
        if self.quote is None:
            return "no query was made"
        if not self.quote.searched:
            return self.quote.unavailable or "the trade search could not run"
        if self.chaos is None:
            return (
                "no listings matched what you ticked — untick a mod and ask again"
                if self.selection.asks_anything
                else "no listings matched"
            )
        if self.thin:
            return (
                f"median of {len(self.quote.listings)} of {self.comparables} matching "
                "listing(s) — too few to trust as a market price"
            )
        return f"median of {len(self.quote.listings)} of {self.comparables} matching listings"

    def to_json(self) -> dict[str, Any]:
        return {
            "uid": self.highlight.uid,
            "name": self.highlight.name,
            "league": self.league,
            "chaos": round(self.chaos, 4) if self.chaos is not None else None,
            "divine": (
                round(self.chaos / self.divine_rate, 4)
                if self.chaos is not None and self.divine_rate
                else None
            ),
            "priced": self.priced,
            "thin": self.thin,
            "comparables": self.comparables,
            "reason": self.reason,
            "spent": self.spent,
            "selection": self.selection.to_json(),
            "quote": self.quote.to_json() if self.quote is not None else None,
        }

    def __repr__(self) -> str:
        return f"PriceCheck({self.highlight.name!r}, {self.chaos}, n={self.comparables})"


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
    def highlighted(self) -> bool:
        """Is this item worth *asking* about?

        The whole of what the gate now claims. It was called ``escalate`` while
        `appraisal` acted on it by spending trade requests unasked; the rename is the
        pivot in one word — nothing acts on this but the player.
        """
        return self.gate.passed

    @property
    def unchecked(self) -> bool:
        """Highlighted, has no price, and nobody has asked the market yet.

        The row the bag total is missing, and the reason
        :attr:`BagAppraisal.total_is_floor` can be true with no query outstanding.
        """
        return self.highlighted and self.valuation.unpriceable and not self.valuation.tier3.answered

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
            "highlighted": self.highlighted,
            "unchecked": self.unchecked,
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
        """Trade-API requests this pass made. **Always zero.**

        Since Phase 9 an appraise makes one account request and no trade requests at
        all: there is no escalation path left to switch on. The field stays because a
        test asserting the zero is worth more than a comment claiming it, and because
        a future phase that reintroduces a request has to change a number a test is
        watching."""

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
    def highlighted(self) -> list[ItemVerdict]:
        """Rows worth asking the market about. A proposal, never an action."""
        return [item for item in self.items if item.highlighted]

    @property
    def unchecked(self) -> list[ItemVerdict]:
        """Highlighted rows with no price and no check behind them.

        These are what the bag total does not include and never will until somebody
        presses the button. Naming them is the honesty that replaces the eager pass:
        the old code hid this hole by spending requests to fill it, badly.
        """
        return [item for item in self.items if item.unchecked]

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
        """``≥`` means "a number is missing and could still arrive".

        Two ways that is true, and neither is "a finished-and-empty search", which
        moves nothing: a query is outstanding, or a highlighted rare has not been
        checked yet. The second is new in Phase 9 and it is the honest replacement for
        the eager pass — the tool used to make that hole small by spending requests on
        it; now it says the hole is there and offers a button.
        """
        return bool(self.pricing) or bool(self.unchecked)

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
                not item.highlighted,
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
            "highlighted_count": len(self.highlighted),
            "unchecked_count": len(self.unchecked),
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
    ) -> BagAppraisal:
        """Verdict every item. ``None`` means "use the configured value".

        **Makes no trade requests, ever.** There is no ``escalate`` parameter any
        more, and no code path it could have switched on: bulk pricing stays automatic
        because poe.ninja works, is free and needs no input, and rares are highlighted
        rather than priced. A rare that comes back without a number has not been
        undervalued — it has been left for the player to ask about.

        ``league`` is the league the items are in — ``ItemSet.league`` — and
        ``override`` deliberately prices them against a different one. Both go
        straight to ``PricesApi``, which refuses to price anything it cannot place in
        an economy; nothing here invents one.
        """
        ...

    def highlight(
        self,
        item: NormalizedItem,
        *,
        strictness: Strictness | None = None,
    ) -> ItemHighlight:
        """The proposal for one item: why it is interesting, and what to tick.

        Synchronous and offline — `moddb` is a local file — so a surface may call it
        the moment a cell is focused without spending anything.
        """
        ...

    async def price_check(
        self,
        item: NormalizedItem,
        selection: Selection | None = None,
        *,
        league: str | None = None,
        override: str | None = None,
        sample: int = 0,
    ) -> PriceCheck:
        """Ask the market **the player's** question. The only method here that spends.

        ``selection`` is what they ticked; ``None`` uses the pre-ticked set, which is
        a convenience for a CLI and not a licence to run this unasked. Two trade
        requests normally, one search and one fetch, and no automatic broadening —
        "nothing matched what you ticked" is an answer, and the fix for it is a tick.
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
