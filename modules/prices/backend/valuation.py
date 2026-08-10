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
"""

from __future__ import annotations

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
        if self.exchange_attempted and exchangeable(item):
            return "not in the poe.ninja index, and no bulk-exchange offers for it"
        return "not in the poe.ninja index for this league"

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


