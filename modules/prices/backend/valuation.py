"""Resolution order, matching, deduplication, stack maths.

Pure functions over loaded tables. Nothing here can perform I/O, which is the point:
:meth:`PricesApi.value_all` must be safe to run on every zone transition without
spending a single request, and the way to guarantee that is for the code that does
the work to have no way of making one.

## The order

Tier 0 (the player's own note) → tier 1 (a poe.ninja bulk table) → tier 1b (a bulk
exchange rate the caller fetched first) → ``unpriceable``.

Tier 1b arrives as a plain ``{name: chaos}`` mapping the caller has already
resolved, which is what keeps this file free of I/O: :class:`PriceIndex` still
cannot make a request, and the network cost of the fallback is visible at the call
site instead of buried in a lookup. Tier 3 never appears here at all — a trade quote
is applied to a finished :class:`~modules.prices.backend.api.Valuation` afterwards.

Note first, per the phase brief. It is defensible on its own terms — the player
looked at the item and decided — but it is also the reason both prices are kept side
by side on the :class:`~modules.prices.backend.api.Valuation` rather than one
overwriting the other. A note is an *asking* price, and Phase 5 wants to show the
player where theirs and the market's disagree.

## Why routing is a preference and not a lookup

``normalize.py`` derives an item's category from its icon art path, which puts
scarabs, fossils, essences, oils, delirium orbs and incubators all under
``2DItems/Currency`` and therefore all in the ``currency`` category — they are
``frameType: 5`` in game and there is nothing else to distinguish them by without a
name table this project refuses to maintain. So :func:`candidate_tables` returns an
*ordered preference*, and the search falls through to every loaded table if the
preferred ones miss. Being wrong about which table an item lives in then costs a
little work, not a wrong price.

## Which line, when several match

poe.ninja lists ``Pillar of the Caged God`` six times — two base types by three link
counts — and ``Map (Tier 16)`` thirteen times, once per map series. Scoring
(:func:`choose_line`) prefers an exact base type and an exact link count, refuses a
corruption mismatch, and otherwise breaks the tie on **listing count**: the most
liquid line is the current one, which is what makes the map series resolve to this
league's without anything here knowing what a map series is.

## Gems, where scoring is exactly the wrong tool

Everything above is a *preference*: a near miss costs a little accuracy. Skill gems
are the case where that trade is unacceptable and the two paths are kept apart
because of it. A level 21 / 20% Cyclone is worth orders of magnitude more than the
level 1 that shares its name, poe.ninja's grid of variants is sparse, and the
best-scoring row for a gem it does not list is always some other gem's price.

So :func:`gem_line` matches ``(level, quality, corrupted)`` exactly or returns
nothing, and never falls through to another table. "Unpriceable" is the correct
answer for a level 19 / 12% gem, and it is the answer this file gives.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from modules.poeapi.backend.api import NormalizedItem, Rarity
from modules.prices.backend.api import (
    BagValuation,
    LeagueSource,
    Price,
    PriceSource,
    TableStatus,
    Valuation,
)
from modules.prices.backend.ninja import PriceLine, PriceTable
from modules.prices.backend.notes import NotePrice, parse_note

__all__ = [
    "DIVINE_TRADE_ID",
    "EXCHANGEABLE_CATEGORIES",
    "PriceIndex",
    "candidate_tables",
    "choose_line",
    "exchangeable",
    "lookup_keys",
    "price_key",
]

DIVINE_TRADE_ID = "divine"

# Ordered table preferences by normalized category. The tail of every list is "and
# then anything", supplied by the resolver — see the module docstring.
_UNIQUE_TABLES: dict[str, tuple[str, ...]] = {
    "weapon": ("unique_weapon",),
    "armour": ("unique_armour",),
    "accessory": ("unique_accessory",),
    "flask": ("unique_flask",),
    "tincture": ("unique_tincture", "unique_flask"),
    "jewel": ("unique_jewel", "cluster_jewel"),
    "map": ("unique_map", "map"),
    "relic": ("unique_relic",),
}

GEM_TABLES: tuple[str, ...] = ("skill_gem",)
"""The only table a gem is ever looked up in, and it is exclusive rather than
preferred.

Every other category falls through to "then try every other loaded table", which is
harmless when names are unique across tables. For a gem it would not be: a name-only
hit in some other overview is exactly the confident wrong answer that kept gems
unpriced, so :meth:`PriceIndex.market_line` refuses the fallback for them."""

_CURRENCY_TABLES: tuple[str, ...] = (
    "currency",
    "fragment",
    "scarab",
    "fossil",
    "resonator",
    "essence",
    "oil",
    "delirium_orb",
    "incubator",
    "artifact",
    "omen",
    "tattoo",
    "allflame_ember",
    "runegraft",
)

_CATEGORY_TABLES: dict[str, tuple[str, ...]] = {
    "currency": _CURRENCY_TABLES,
    "fragment": ("fragment", "scarab", "currency"),
    "card": ("card",),
    "map": ("map", "unique_map", "blighted_map"),
    "jewel": ("cluster_jewel", "unique_jewel"),
    "gem": GEM_TABLES,
}


def candidate_tables(item: NormalizedItem) -> tuple[str, ...]:
    """Table keys to try for ``item``, most likely first."""
    if item.rarity in (Rarity.UNIQUE, Rarity.RELIC):
        preferred = _UNIQUE_TABLES.get(item.category)
        if preferred:
            return preferred
    return _CATEGORY_TABLES.get(item.category, ())


def lookup_keys(item: NormalizedItem) -> list[str]:
    """Names to look the item up by, most specific first.

    Maps are the awkward case. In game a map is *Grotto Map* with a ``Map Tier``
    property; poe.ninja indexes ordinary maps of a tier as one line called
    ``Map (Tier 16)`` and names only the special ones (``Drox Map (Tier 16)``). So the
    named forms are tried first and the generic tier line is the fallback.
    """
    keys: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in keys:
            keys.append(value)

    if item.category in {"map", "fragment"} and item.map_tier:
        add(f"{item.name} (Tier {item.map_tier})")
        add(item.name)
        add(f"Map (Tier {item.map_tier})")
    else:
        add(item.name)
        add(item.base_type)
    return keys


def price_key(item: NormalizedItem) -> tuple:
    """Everything that can change an item's price, and nothing else.

    Two items with the same key get **one** lookup between them, which is what
    SPEC §5.1's "deduplicate before pricing" means. ``uid``, grid position, stack size
    and ilvl are all absent on purpose: none of them changes what the item is worth
    per unit.
    """
    return (
        item.name,
        item.base_type,
        item.category,
        item.rarity.value,
        item.corrupted,
        item.sockets.links,
        item.map_tier,
        # Two gems of the same name and different level are two different prices, so
        # they must not share a lookup. Without this, a tab holding a level 1 and a
        # level 21 Cyclone would value the second as the first — which is exactly the
        # failure the gem work exists to prevent, arriving through the cache instead
        # of through the match.
        (item.gem.level, item.gem.quality) if item.gem is not None else None,
        item.note,
    )


def choose_line(lines: Sequence[PriceLine], item: NormalizedItem) -> PriceLine | None:
    """Pick the line that best describes ``item``. See the module docstring."""
    if not lines:
        return None
    best: PriceLine | None = None
    best_score: tuple[int, int, int, float] | None = None
    for line in lines:
        score = 0
        if line.base_type and item.base_type:
            score += 8 if line.base_type.casefold() == item.base_type.casefold() else -8
        if line.links is not None:
            score += 4 if line.links == item.sockets.links else -4
        elif item.sockets.links >= 5:
            # The linkless line is the price of the item without its links; using it
            # for a 5- or 6-link understates a real difference.
            score -= 2
        if line.corrupted is not None:
            score += 2 if line.corrupted is item.corrupted else -6
        candidate = (score, line.listing_count, line.count, -line.chaos)
        if best_score is None or candidate > best_score:
            best, best_score = line, candidate
    return best


# -- gems -----------------------------------------------------------------------
#
# A gem is the one thing in a bag whose name does not identify it. poe.ninja prices
# skill gems per *variant*, and the variant is three facts: level, quality, and
# whether it is corrupted. Measured against Allflame's `SkillGem` table on
# 2026-08-11 — 7 519 rows, 27 distinct variants — the grammar is exactly:
#
#     "20"        level 20, quality 0,  not corrupted
#     "20/20"     level 20, quality 20, not corrupted
#     "21c"       level 21, quality 0,  corrupted
#     "21/23c"    level 21, quality 23, corrupted
#
# and nothing else appears. Two invariants held across every row and both are checked
# rather than trusted: the trailing ``c`` agrees with the row's own ``corrupted``
# flag (0 disagreements), and the leading number agrees with ``gemLevel``
# (0 disagreements).
#
# The table is a **sparse grid**, and that is the whole reason matching has to be
# exact rather than nearest. Cyclone has eleven rows — 1, 1/20, 1/23c, 20, 20c,
# 20/20, 20/20c, 20/23c, 21c, 21/20c, 21/23c — and a level 19 / 12% Cyclone is none
# of them. The nearest row by any metric is 20/20, which is worth many times more.
# So a variant with no row is `unpriceable`, and says so.

_GEM_VARIANT = re.compile(r"^(\d+)(?:/(\d+))?(c?)$")

GemVariant = tuple[int, int, bool]
"""``(level, quality, corrupted)``."""


def gem_variant(line: PriceLine) -> GemVariant | None:
    """A gem row's variant, or ``None`` if the row cannot be trusted to have one.

    ``None`` for anything that is not a parseable gem variant, and — deliberately —
    for a row whose ``variant`` and ``gemLevel`` disagree. A contradiction between
    the two fields is not a thing to resolve by preferring one; it means poe.ninja's
    row does not describe a single gem, and pricing an item against it would be
    inventing the answer.
    """
    if not line.variant:
        return None
    match = _GEM_VARIANT.match(line.variant.strip())
    if match is None:
        return None
    level = int(match.group(1))
    quality = int(match.group(2) or 0)
    corrupted = bool(match.group(3))
    if line.gem_level is not None and line.gem_level != level:
        return None
    if line.corrupted is not None and bool(line.corrupted) != corrupted:
        return None
    return level, quality, corrupted


def wanted_variant(item: NormalizedItem) -> GemVariant | None:
    """The variant *this gem is*, or ``None`` when the item cannot say.

    A gem whose level did not survive the wire has no variant, and therefore no
    price. That is the honest outcome: the alternative is picking the level-1 row
    because it sorts first.
    """
    if item.gem is None or item.gem.level is None:
        return None
    return item.gem.level, item.gem.quality, item.corrupted


def gem_line(lines: Sequence[PriceLine], item: NormalizedItem) -> PriceLine | None:
    """The row for *exactly* this gem, or ``None``.

    No scoring, no nearest match, no tie-break by listing count. Either one row in
    the table is this level, this quality and this corruption state, or the gem is
    unpriceable. Two rows claiming the same variant is also ``None`` — poe.ninja has
    never produced one, and if it did, choosing between them would be a guess.
    """
    want = wanted_variant(item)
    if want is None:
        return None
    matched = [line for line in lines if gem_variant(line) == want]
    return matched[0] if len(matched) == 1 else None


def describe_variant(item: NormalizedItem) -> str:
    """``"level 21, 20% quality, corrupted"`` — for the reason on an unpriced gem."""
    if item.gem is None or item.gem.level is None:
        return "no readable gem level"
    parts = [f"level {item.gem.level}", f"{item.gem.quality}% quality"]
    if item.corrupted:
        parts.append("corrupted")
    return ", ".join(parts)


# Categories for which a bulk-exchange rate is a sensible answer. The exchange
# trades currency and fragments in stacks; it does not price a rare ring, and asking
# it to would be tier 3's job done badly.
EXCHANGEABLE_CATEGORIES: frozenset[str] = frozenset({"currency", "fragment", "card"})


def exchangeable(item: NormalizedItem) -> bool:
    """Whether tier 1b could conceivably price ``item``.

    Rarity as well as category: ``normalize.py`` derives the category from icon art,
    and a *unique* whose art lives under ``2DItems/Currency`` would otherwise be sent
    to an endpoint that has never heard of it.
    """
    if item.rarity in (Rarity.UNIQUE, Rarity.RELIC, Rarity.RARE, Rarity.MAGIC):
        return False
    return item.category in EXCHANGEABLE_CATEGORIES


@dataclass
class PriceIndex:
    """Every loaded table, plus the two lookups the resolver needs."""

    tables: Mapping[str, PriceTable]
    league: str = ""
    exchange_rates: Mapping[str, Price] = field(default_factory=dict)
    """Tier 1b, by item name, resolved by the caller before this index was built.
    Empty is the normal case: it is only populated for names tier 1 missed."""

    exchange_attempted: bool = False
    """Whether the caller actually asked the bulk exchange. Distinct from
    ``exchange_rates`` being empty, which happens both when we asked and got nothing
    and when we never asked — and those want different words on an unpriceable row."""

    def table(self, key: str) -> PriceTable | None:
        return self.tables.get(key)

    def fetched_at(self, key: str) -> datetime | None:
        table = self.tables.get(key)
        if table is None:
            return None
        return datetime.fromtimestamp(table.fetched_at, tz=UTC)

    # -- tier 0 ----------------------------------------------------------------

    def chaos_for_trade_id(self, trade_id: str) -> float | None:
        """Chaos value of one trade id, from whichever exchange table carries it.

        ``chaos`` itself is not a line in the currency overview — it is the unit — so
        it is answered here rather than looked up.
        """
        token = trade_id.casefold()
        if token == "chaos":
            return 1.0
        for table in self.tables.values():
            line = table.by_trade_id.get(token)
            if line is not None:
                return line.chaos
        return None

    @property
    def chaos_per_divine(self) -> float | None:
        return self.chaos_for_trade_id(DIVINE_TRADE_ID)

    def note_price(self, item: NormalizedItem) -> tuple[Price | None, NotePrice | None]:
        """Tier 0. Returns ``(price, parsed)`` — ``parsed`` survives an unknown
        currency so a surface can still show what the player wrote."""
        parsed = parse_note(item.note)
        if parsed is None:
            return None, None
        rate = self.chaos_for_trade_id(parsed.currency)
        if rate is None:
            return None, parsed
        return (
            Price(
                parsed.amount * rate,
                PriceSource.NOTE,
                detail=parsed.text,
                as_of=self.fetched_at("currency"),
            ),
            parsed,
        )

    # -- tier 1 ----------------------------------------------------------------

    def market_price(self, item: NormalizedItem) -> Price | None:
        """Tier 1. ``None`` means *not in the index* — never *worth nothing*."""
        line = self.market_line(item)
        if line is None:
            return None
        return Price(
            line.chaos,
            PriceSource.BULK,
            category=line.category,
            detail=line.detail() or None,
            listing_count=line.listing_count or None,
            as_of=self.fetched_at(line.category),
        )

    def market_line(self, item: NormalizedItem) -> PriceLine | None:
        keys = [key.casefold() for key in lookup_keys(item)]
        if not keys:
            return None
        if item.category == "gem":
            return self._gem_line(item, keys)
        preferred = candidate_tables(item)
        rest = [key for key in self.tables if key not in preferred]
        for key in keys:
            for table_key in (*preferred, *rest):
                table = self.tables.get(table_key)
                if table is None:
                    continue
                chosen = choose_line(table.by_name.get(key, ()), item)
                if chosen is not None:
                    return chosen
        return None

    def _gem_line(self, item: NormalizedItem, keys: Sequence[str]) -> PriceLine | None:
        """The gem path: one table, exact variant, no fallback.

        Split out rather than folded into the loop above because every step of that
        loop is a compromise a gem cannot afford — a second table to try, a scored
        best-of, a tie broken on liquidity. Here the answer is a row that *is* this
        gem, or nothing.
        """
        for table_key in GEM_TABLES:
            table = self.tables.get(table_key)
            if table is None:
                continue
            for key in keys:
                matched = gem_line(table.by_name.get(key, ()), item)
                if matched is not None:
                    return matched
        return None

    # -- the resolution order --------------------------------------------------

    # -- tier 1b ---------------------------------------------------------------

    def exchange_price(self, item: NormalizedItem) -> Price | None:
        """Tier 1b. A rate the caller fetched from GGG's bulk exchange, or ``None``."""
        if not self.exchange_rates or not exchangeable(item):
            return None
        for key in (item.name, item.base_type):
            if key and key in self.exchange_rates:
                return self.exchange_rates[key]
        return None

    def _no_price_reason(self, item: NormalizedItem) -> str:
        """Why nothing priced it, in the terms of whatever *should* have.

        "Not in the index" is the right sentence for a Veiled Scarab and the wrong
        one for a rare ring, and after this phase there is a third case: something
        the index misses *and* nobody is bulk-selling. Saying which is what keeps
        ``unpriceable`` a statement about our knowledge rather than a shrug.
        """
        if item.category == "gem":
            return self._no_gem_price_reason(item)
        if self.exchange_attempted and exchangeable(item):
            return "not in the poe.ninja index, and no bulk-exchange offers for it"
        return "not in the poe.ninja index for this league"

    def _no_gem_price_reason(self, item: NormalizedItem) -> str:
        """Three different silences, told apart.

        "Unpriceable" was one word for all of them until now, and the three want
        different actions: refresh, nothing, and read the item again. Naming which is
        the difference between a tool that is being careful and a tool that is broken.
        """
        if not any(key in self.tables for key in GEM_TABLES):
            return "the poe.ninja skill gem table has not been loaded for this league"
        if wanted_variant(item) is None:
            return "no gem level on this item, so no variant to match"
        return (
            f"poe.ninja lists no {describe_variant(item)} variant of this gem; "
            "refusing to price it as a different one"
        )

    # -- the resolution order --------------------------------------------------

    def value(self, item: NormalizedItem) -> Valuation:
        note, parsed = self.note_price(item)
        market = self.market_price(item)
        exchange = self.exchange_price(item) if market is None else None
        price = note or market or exchange
        reason = None
        if price is None:
            reason = (
                f"note {parsed.text!r} names a currency the price tables do not carry"
                if parsed is not None
                else self._no_price_reason(item)
            )
        return Valuation(
            uid=item.uid,
            name=item.name,
            base_type=item.base_type,
            category=item.category,
            stack_size=item.stack_size,
            price=price,
            note_price=note,
            market=market or exchange,
            reason=reason,
        )

    def value_all(
        self,
        items: Iterable[NormalizedItem],
        *,
        table_status: TableStatus | None = None,
        league_source: LeagueSource | None = None,
        exchange_requests: int = 0,
    ) -> BagValuation:
        """Price a bag. One lookup per distinct :func:`price_key`, fanned out."""
        resolved: dict[tuple, Valuation] = {}
        out: list[Valuation] = []
        for item in items:
            key = price_key(item)
            template = resolved.get(key)
            if template is None:
                template = self.value(item)
                resolved[key] = template
            out.append(
                Valuation(
                    uid=item.uid,
                    name=template.name,
                    base_type=template.base_type,
                    category=template.category,
                    stack_size=item.stack_size,
                    price=template.price,
                    note_price=template.note_price,
                    market=template.market,
                    reason=template.reason,
                )
            )
        return BagValuation(
            out,
            league=self.league,
            league_source=league_source,
            divine_rate=self.chaos_per_divine,
            table=table_status,
            lookups=len(resolved),
            trade_requests=0,
            exchange_requests=exchange_requests,
        )


