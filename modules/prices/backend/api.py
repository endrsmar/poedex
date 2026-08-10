"""The public surface of the `prices` module.

**This is the only file in this module that other modules may import** (plan §1.4,
enforced by ``tests/test_boundaries.py``).

Three decisions are encoded in these types rather than left to a caller, because
getting them wrong is what makes a bag total a lie:

* **``unpriceable`` is a state, not a zero.** :class:`Valuation` carries a
  :class:`PriceSource` and an explicit flag; a totals row reports the unpriceable
  *stack count* alongside the chaos figure. The live account holds ~170 of a removed
  item absent from poe.ninja's index (research-notes §7), and folding those into the
  total as worth nothing understates the stash badly.
* **Value is per stack, not per row.** ``Jeweller's Orb x2615`` and ``Divine Orb x5``
  are each one row and wildly different totals, so :class:`Valuation` keeps the unit
  price and the stack size apart and multiplies them itself.
* **The player's own ``note`` is both a fallback and a separate signal.** It resolves
  first (tier 0, SPEC §5.0) *and* stays visible next to the market price, because
  "your price vs market" is a Phase 5 screen and the comparison is impossible once
  the two have been collapsed into one number.

Everything here is chaos-denominated. Chaos is what poe.ninja quotes both its
exchange overview (``core.primary == "chaos"``) and its item overviews in, and mixing
denominations inside the model would make every sum a conversion bug waiting to
happen. :attr:`BagValuation.divine_rate` is carried alongside so a surface can render
a divine figure without doing arithmetic on prices it did not compute.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from modules.poeapi.backend.api import NormalizedItem
from runtime.errors import PoedexError

__all__ = [
    "PRICES_UPDATED",
    "BagValuation",
    "Price",
    "PriceSource",
    "PricesApi",
    "PricesError",
    "TableStatus",
    "TradeQuote",
    "TradeUnavailable",
    "Valuation",
]

PRICES_UPDATED = "prices_updated"
"""Event topic emitted after the bulk tables are refreshed. Payload carries the
league, how many tables are loaded, and how many changed."""


class PricesError(PoedexError):
    """A problem fetching or interpreting price data."""


class TradeUnavailable(PricesError):
    """A tier-3 quote could not be produced.

    Distinct from "the item is worth nothing" and from "the item is not in the bulk
    index": it means the *query* failed — refused by the limiter, no online sellers,
    or an item shape the query builder cannot express.
    """


class PriceSource(StrEnum):
    """Which tier produced a number. SPEC §5."""

    NOTE = "note"
    """Tier 0 — the player's own ``~price`` / ``~b/o`` tag. Free with every fetch."""

    BULK = "bulk"
    """Tier 1 — a poe.ninja overview table."""

    TRADE = "trade"
    """Tier 3 — a live trade query. On demand only; never during a valuation pass."""

    NONE = "unpriceable"
    """No tier resolved. **Not** the same as zero."""


class Price:
    """One resolved unit price, in chaos.

    A plain class rather than a pydantic model because it is small, immutable and
    constructed in tight loops; :meth:`to_json` is the frontend boundary.
    """

    __slots__ = (
        "as_of",
        "category",
        "chaos",
        "detail",
        "listing_count",
        "sample_size",
        "source",
    )

    def __init__(
        self,
        chaos: float,
        source: PriceSource,
        *,
        category: str | None = None,
        detail: str | None = None,
        listing_count: int | None = None,
        sample_size: int | None = None,
        as_of: datetime | None = None,
    ) -> None:
        self.chaos = float(chaos)
        self.source = source
        self.category = category
        self.detail = detail
        """How the line was identified — the matched variant, links, or the note's
        own text. Shown in a detail pane; never parsed."""

        self.listing_count = listing_count
        self.sample_size = sample_size
        self.as_of = as_of

    def scaled(self, quantity: int) -> float:
        return self.chaos * quantity

    def to_json(self) -> dict[str, Any]:
        return {
            "chaos": round(self.chaos, 4),
            "source": self.source.value,
            "category": self.category,
            "detail": self.detail,
            "listing_count": self.listing_count,
            "sample_size": self.sample_size,
            "as_of": self.as_of.isoformat() if self.as_of else None,
        }

    def __repr__(self) -> str:
        return f"Price({self.chaos:g}c, {self.source.value})"


class Valuation:
    """What one item is worth, and how that was decided."""

    __slots__ = (
        "base_type",
        "category",
        "market",
        "name",
        "note_price",
        "price",
        "reason",
        "stack_size",
        "uid",
    )

    def __init__(
        self,
        *,
        uid: str,
        name: str,
        base_type: str,
        category: str,
        stack_size: int,
        price: Price | None,
        note_price: Price | None = None,
        market: Price | None = None,
        reason: str | None = None,
    ) -> None:
        self.uid = uid
        self.name = name
        self.base_type = base_type
        self.category = category
        self.stack_size = max(1, int(stack_size))
        self.price = price
        self.note_price = note_price
        """Tier 0, kept even when tier 1 won, for the "your price vs market" diff."""

        self.market = market
        """Tier 1, kept even when the note won, for the same reason."""

        self.reason = reason

    @property
    def unpriceable(self) -> bool:
        return self.price is None

    @property
    def source(self) -> PriceSource:
        return self.price.source if self.price else PriceSource.NONE

    @property
    def total_chaos(self) -> float:
        """Stack-aware. ``0.0`` when unpriceable — read :attr:`unpriceable` first."""
        return self.price.scaled(self.stack_size) if self.price else 0.0

    @property
    def overpriced_by(self) -> float | None:
        """``note / market``, or ``None`` unless both exist. Phase 5's comparison."""
        if self.note_price is None or self.market is None or not self.market.chaos:
            return None
        return self.note_price.chaos / self.market.chaos

    def to_json(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "base_type": self.base_type,
            "category": self.category,
            "stack_size": self.stack_size,
            "unpriceable": self.unpriceable,
            "source": self.source.value,
            "total_chaos": round(self.total_chaos, 4),
            "price": self.price.to_json() if self.price else None,
            "note_price": self.note_price.to_json() if self.note_price else None,
            "market": self.market.to_json() if self.market else None,
            "overpriced_by": self.overpriced_by,
            "reason": self.reason,
        }

    def __repr__(self) -> str:
        state = "unpriceable" if self.unpriceable else f"{self.total_chaos:g}c"
        return f"Valuation({self.name!r} x{self.stack_size}, {state})"


class BagValuation:
    """Every item, plus the totals a surface shows without recomputing anything."""

    __slots__ = ("divine_rate", "items", "league", "lookups", "table", "trade_requests")

    def __init__(
        self,
        items: Sequence[Valuation],
        *,
        league: str,
        divine_rate: float | None = None,
        table: TableStatus | None = None,
        lookups: int = 0,
        trade_requests: int = 0,
    ) -> None:
        self.items = list(items)
        self.league = league
        self.divine_rate = divine_rate
        """Chaos per Divine Orb, from the currency table. ``None`` if it is missing."""

        self.table = table
        self.lookups = lookups
        """Distinct price resolutions performed. Lower than ``len(items)`` whenever
        deduplication did its job (SPEC §5.1)."""

        self.trade_requests = trade_requests
        """Trade-API requests this pass made. **Always zero**: tier 3 is on demand
        only (SPEC §5.3), and this field is here so a test can assert it."""

    @property
    def priced(self) -> list[Valuation]:
        return [v for v in self.items if not v.unpriceable]

    @property
    def unpriceable(self) -> list[Valuation]:
        return [v for v in self.items if v.unpriceable]

    @property
    def total_chaos(self) -> float:
        return sum(v.total_chaos for v in self.items)

    @property
    def total_divine(self) -> float | None:
        if not self.divine_rate:
            return None
        return self.total_chaos / self.divine_rate

    @property
    def unpriceable_stack(self) -> int:
        """Units, not rows. ~170 of one removed item is one row and a real hole."""
        return sum(v.stack_size for v in self.unpriceable)

    def to_json(self) -> dict[str, Any]:
        return {
            "league": self.league,
            "items": [v.to_json() for v in self.items],
            "total_chaos": round(self.total_chaos, 4),
            "total_divine": (
                round(self.total_divine, 4) if self.total_divine is not None else None
            ),
            "divine_rate": self.divine_rate,
            "priced_count": len(self.priced),
            "unpriceable_count": len(self.unpriceable),
            "unpriceable_stack": self.unpriceable_stack,
            "lookups": self.lookups,
            "trade_requests": self.trade_requests,
            "table": self.table.to_json() if self.table else None,
        }


class TableStatus:
    """Freshness of the bulk tables, carried with every valuation."""

    __slots__ = ("league", "loaded", "newest", "note", "oldest", "requested", "stale")

    def __init__(
        self,
        *,
        league: str,
        loaded: int,
        requested: int,
        oldest: datetime | None = None,
        newest: datetime | None = None,
        stale: bool = False,
        note: str | None = None,
    ) -> None:
        self.league = league
        self.loaded = loaded
        self.requested = requested
        self.oldest = oldest
        self.newest = newest
        self.stale = stale
        self.note = note

    @property
    def healthy(self) -> bool:
        return self.loaded > 0 and not self.stale

    def to_json(self) -> dict[str, Any]:
        return {
            "league": self.league,
            "loaded": self.loaded,
            "requested": self.requested,
            "oldest": self.oldest.isoformat() if self.oldest else None,
            "newest": self.newest.isoformat() if self.newest else None,
            "stale": self.stale,
            "note": self.note,
        }


class TradeQuote:
    """A tier-3 answer: the median of the cheapest N **online** listings.

    Median rather than minimum, deliberately (SPEC §5.3). The cheapest listing on the
    official site is very often a bot, a mispriced item, or someone who logged off
    three weeks ago; pricing against it produces a number the player cannot actually
    realise.
    """

    __slots__ = ("chaos", "considered", "listings", "online", "query_url", "total")

    def __init__(
        self,
        chaos: float | None,
        *,
        considered: int,
        online: int,
        total: int,
        listings: Sequence[float] = (),
        query_url: str | None = None,
    ) -> None:
        self.chaos = chaos
        self.considered = considered
        """Listings fetched. Capped at 10 — the fetch endpoint's own limit."""

        self.online = online
        self.total = total
        """What the search said the whole result set was, before any fetch."""

        self.listings = list(listings)
        self.query_url = query_url

    def to_json(self) -> dict[str, Any]:
        return {
            "chaos": round(self.chaos, 4) if self.chaos is not None else None,
            "considered": self.considered,
            "online": self.online,
            "total": self.total,
            "listings": [round(v, 4) for v in self.listings],
            "query_url": self.query_url,
        }


@runtime_checkable
class PricesApi(Protocol):
    """What dependents get from ``ctx.require(PricesApi)``.

    ``value`` and ``value_all`` are guaranteed **not** to touch the network: they read
    tables that were prefetched at module start. That guarantee is what lets an
    appraisal screen price a bag on a zone transition without spending a request, and
    it is why tier 3 is a separate method with a name that sounds expensive.
    """

    async def value(self, item: NormalizedItem) -> Valuation:
        """Price one item: tier 0 note, then tier 1 bulk, then ``unpriceable``."""
        ...

    async def value_all(self, items: Sequence[NormalizedItem]) -> BagValuation:
        """Price a whole bag, deduplicated, with stack-aware totals."""
        ...

    async def bulk(self, category: str) -> Mapping[str, Price]:
        """One bulk table as ``name -> Price``. Fetches it if it is not loaded."""
        ...

    async def refresh(self, *, force: bool = False) -> TableStatus:
        """Refresh the bulk tables with conditional requests. Costs no GGG budget."""
        ...

    def status(self) -> TableStatus:
        """Table freshness, without fetching anything."""
        ...

    async def quote(self, item: NormalizedItem, *, sample: int = 0) -> TradeQuote:
        """Tier 3. **On demand only** — never call this from a valuation pass.

        ``sample`` is how many of the cheapest online listings to take the median of;
        ``0`` uses the configured default. Capped at 10 by the fetch endpoint.
        """
        ...
