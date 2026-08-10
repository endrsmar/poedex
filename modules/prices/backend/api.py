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
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from modules.poeapi.backend.api import LeagueUnknownError, NormalizedItem
from runtime.errors import PoedexError

__all__ = [
    "MAX_QUERY_FILTERS",
    "PRICES_UPDATED",
    "BagValuation",
    "LeagueChoice",
    "LeagueSource",
    "LeagueUnknownError",
    "ModFocus",
    "Price",
    "PriceSource",
    "PricesApi",
    "PricesError",
    "QuerySpec",
    "TableStatus",
    "Tier3",
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


class LeagueSource(StrEnum):
    """Where the league a bag was priced against came from.

    Carried all the way to the surface because "Standard" on its own is not an
    answer a player can check. "Standard, because that is the league your character
    is in" and "Standard, because you told me to ignore the character" are different
    claims, and only one of them is a bug when the character is in Allflame.
    """

    ARGUMENT = "argument"
    """A per-run ``--league``. The most explicit thing a caller can say."""

    SETTING = "setting"
    """``prices.league``. A standing instruction to ignore the character."""

    CHARACTER = "character"
    """The bag's own league, from the character it was fetched for. The normal path
    and, since this is the only source that cannot be stale, the right default."""


@dataclass(frozen=True, slots=True)
class LeagueChoice:
    """A resolved league and the reason it won."""

    league: str
    source: LeagueSource

    @property
    def overridden(self) -> bool:
        """``True`` when something other than the character decided.

        A surface should say so. Pricing an Allflame bag against Standard is a
        legitimate thing to ask for and an illegitimate thing to do by accident, and
        the only difference visible from the outside is whether it was announced.
        """
        return self.source is not LeagueSource.CHARACTER

    def describe(self) -> str:
        if self.source is LeagueSource.CHARACTER:
            return f"{self.league} (from the character)"
        origin = "--league" if self.source is LeagueSource.ARGUMENT else "prices.league setting"
        return f"{self.league} (OVERRIDE via {origin} — not the character's league)"

    def to_json(self) -> dict[str, Any]:
        return {
            "league": self.league,
            "source": self.source.value,
            "overridden": self.overridden,
        }


class PriceSource(StrEnum):
    """Which tier produced a number. SPEC §5.

    Five values for four tiers, because tier 1 has two sources now and they are not
    interchangeable: a poe.ninja line is a whole-market index refreshed every fifteen
    minutes, and an exchange rate is the median of ten live offers on a thin market.
    Collapsing them would leave a surface unable to say which of those it is showing.
    """

    NOTE = "note"
    """Tier 0 — the player's own ``~price`` / ``~b/o`` tag. Free with every fetch."""

    BULK = "bulk"
    """Tier 1 — a poe.ninja overview table."""

    EXCHANGE = "exchange"
    """Tier 1b — GGG's bulk exchange, for currency the bulk index does not carry."""

    TRADE = "trade"
    """Tier 3 — a live trade search. Eager for a bag's gated rares, never for a
    stash (SPEC §5.3)."""

    NONE = "unpriceable"
    """No tier resolved. **Not** the same as zero."""


class Tier3(StrEnum):
    """What happened to this item's tier-3 query. Five states, and the middle three
    are the ones a boolean could not tell apart.

    The first live appraisal rendered ``pricing…`` next to two rares whose searches
    had already come back with **zero results**. A single ``pricing`` flag cannot say
    the difference between *"the answer is coming"* and *"the answer arrived and it
    was nothing"*, so it said the first about both — which promises a number that is
    never going to appear. "No matching listings" is a real, terminal answer and it
    has to be able to say so.
    """

    NONE = "none"
    """No tier-3 query was made for this item. The default, and the state of every
    row in a ``value`` pass — which never escalates."""

    PENDING = "pending"
    """Started and still outstanding when the pass returned. The only state that
    justifies ``pricing…``, and the only one that makes the bag total a floor which
    will later move."""

    NO_LISTINGS = "no_listings"
    """The search ran and matched nothing — after the broadening retry. Terminal:
    re-running it today will return the same nothing. The item is not worthless, it
    is *uncompared*, and the surface must say that rather than keep spinning."""

    FAILED = "failed"
    """The query could not be made at all — rate limited, network error, no stat
    index. Also terminal for this pass, but for a reason about us rather than about
    the market, so it gets its own word."""

    PRICED = "priced"
    """Answered with a number, which is now on :attr:`Valuation.price`."""

    @property
    def answered(self) -> bool:
        """Did tier 3 come back? ``NONE`` is not an answer; neither is ``PENDING``."""
        return self in (Tier3.NO_LISTINGS, Tier3.FAILED, Tier3.PRICED)


MAX_QUERY_FILTERS = 6
"""How many mod filters one **chosen** query may carry.

Six, because six is how many affixes a rare has: a player who ticks every line has
asked for a near-exact-match search, and the honest response to that is to run it and
report the one listing it found *as one listing*, not to quietly drop half of it. The
automatic path is capped much lower (``trade.MAX_STAT_FILTERS`` is 2, from
measurement); this number exists because a selection is not a heuristic and must not
be second-guessed."""


@dataclass(frozen=True, slots=True)
class ModFocus:
    """One mod a trade query should filter on, and how tightly.

    Since Phase 9 the caller that fills these in is the **player**, through the
    checkbox list `appraisal` draws: automatic rare pricing failed twice against a
    live account, once by ANDing every mod and matching zero listings, once by
    querying one loose mod and reporting a stranger's asking price as a median. Which
    mods make an item interesting is not derivable from the item, so it is asked.

    ``minimum`` is ``None`` when nothing may be claimed about the roll — an
    unattributed line, or a mod the player ticked purely for its presence.
    """

    text: str
    """The mod line as it appears on the item — ``+103 to maximum Life``. Resolved to
    an opaque stat id by the stat index; a mod that does not resolve is dropped."""

    minimum: float | None = None
    """A widened floor, already computed by the caller. Never the exact roll: an
    equality filter on a random roll matches almost nothing."""

    label: str = ""
    """Human words for the query description, so a wrong query is legible."""

    origin: str = "explicit"
    """Where the line sits on the item — ``explicit``, ``crafted``, ``fractured``,
    ``implicit``, ``enchant``. It selects between ids that share one sentence: a
    crafted ``+# to maximum Life`` searched as an explicit one excludes every item
    whose life roll *is* the bench craft."""

    local: bool | None = None
    """Whether this line is the *local* reading of its sentence — the other axis
    ``origin`` cannot cover, and one the stats document cannot answer for itself.

    Twenty-two sentences are published twice. ``#% increased Armour`` is a global stat on
    a ring and ``#% increased Armour (Local)`` on a body armour, and searching the
    wrong one is not a near miss: measured live in Phase 9b, the global id matched
    **0** rare body armours where the local id matched 10 000+. Only `moddb` can tell
    them apart, because the difference is which game stat the mod carries, so
    :attr:`ModMatch.local` is carried down here rather than re-derived from the text.

    ``None`` means nobody knew — a caller with no `moddb` report, or a line whose
    candidates disagreed. The global reading then wins, except for the six sentences
    that have no global reading at all."""


@dataclass(frozen=True, slots=True)
class QuerySpec:
    """Everything one trade query asks — and nothing the caller did not ask for.

    This is the shape of a **manual price check**. It exists because the query used to
    be assembled from a gate's opinion inside `prices`, and the two ways of doing that
    both failed: every mod ANDed matched nothing, one loose mod matched anything.
    A spec is the player's answer to *which mods make this item interesting*, carried
    down without interpretation.
    """

    mods: tuple[ModFocus, ...] = ()

    open_prefixes: int | None = None
    """"At least N prefix slots still free." A real trade filter and a real source of
    value — an item with an open prefix is one somebody can finish. ``None`` means the
    question is not asked at all, which is different from asking for zero."""

    open_suffixes: int | None = None

    broaden: bool = False
    """Whether a search that matches nothing may be retried with one loose filter.

    ``False`` for a manual check, deliberately. Broadening answers a *different*
    question and reports the answer under the player's heading; "no listings matched
    what you ticked" is the honest outcome and the fix for it is a tick, not a
    silently wider search."""

    limit: int = MAX_QUERY_FILTERS
    """Mod filters this spec may spend. See :data:`MAX_QUERY_FILTERS`."""

    @property
    def asks_anything(self) -> bool:
        return bool(self.mods) or self.open_prefixes is not None or self.open_suffixes is not None


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
        "tier3",
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
        tier3: Tier3 = Tier3.NONE,
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
        self.tier3 = tier3
        """What tier 3 did about this item — see :class:`Tier3`.

        Deliberately not a third state of :attr:`price`. "We are still looking",
        "we looked and found nothing" and "we could not look" want different words on
        screen, and a caller that only reads :attr:`unpriceable` still gets the safe
        answer in all three."""

    @property
    def unpriceable(self) -> bool:
        return self.price is None

    @property
    def pricing(self) -> bool:
        """A tier-3 query for this item is **outstanding**: started, not answered.

        SPEC §5.3 asks for a per-item ``pricing…`` state that never gates the grid,
        and this is it — narrowed, since the first live appraisal, to mean only what
        it says. A search that came back empty is :attr:`no_listings`, not this."""
        return self.tier3 is Tier3.PENDING

    @property
    def no_listings(self) -> bool:
        """Tier 3 answered, and the answer was *nothing matched*. Terminal."""
        return self.tier3 is Tier3.NO_LISTINGS

    @property
    def tier3_failed(self) -> bool:
        """Tier 3 could not run — rate limited, offline, no stat index. Terminal for
        this pass, and about us rather than about the market."""
        return self.tier3 is Tier3.FAILED

    def replace_price(self, price: Price, *, reason: str | None = None) -> Valuation:
        """A copy carrying ``price``. Used when tier 3 lands after tier 1 missed.

        A copy rather than a mutation because ``value_all`` fans one resolution out
        across every row that shares a :func:`~modules.prices.backend.valuation.price_key`,
        and those rows differ in ``uid`` and stack size.
        """
        return Valuation(
            uid=self.uid,
            name=self.name,
            base_type=self.base_type,
            category=self.category,
            stack_size=self.stack_size,
            price=price,
            note_price=self.note_price,
            market=self.market,
            reason=reason,
            tier3=Tier3.PRICED if price.source is PriceSource.TRADE else self.tier3,
        )

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
            "pricing": self.pricing,
            "no_listings": self.no_listings,
            "tier3": self.tier3.value,
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

    __slots__ = (
        "divine_rate",
        "exchange_requests",
        "items",
        "league",
        "league_source",
        "lookups",
        "table",
        "trade_requests",
    )

    def __init__(
        self,
        items: Sequence[Valuation],
        *,
        league: str,
        league_source: LeagueSource | None = None,
        divine_rate: float | None = None,
        table: TableStatus | None = None,
        lookups: int = 0,
        trade_requests: int = 0,
        exchange_requests: int = 0,
    ) -> None:
        self.items = list(items)
        self.league = league
        self.league_source = league_source
        """Why :attr:`league` is what it is. ``None`` only for a valuation built
        straight from a :class:`~modules.prices.backend.valuation.PriceIndex` in a
        test; everything the module produces says where the league came from."""

        self.divine_rate = divine_rate
        """Chaos per Divine Orb, from the currency table. ``None`` if it is missing."""

        self.table = table
        self.lookups = lookups
        """Distinct price resolutions performed. Lower than ``len(items)`` whenever
        deduplication did its job (SPEC §5.1)."""

        self.trade_requests = trade_requests
        """Trade-*search* requests this pass made. Zero unless a caller asked for
        eager tier 3 — which only ``appraisal`` does, only at bag strictness, and
        only for items its gate passed (SPEC §5.3)."""

        self.exchange_requests = exchange_requests
        """Bulk-exchange requests this pass made. Zero when the per-league rate cache
        was warm, which is the normal case."""

    @property
    def priced(self) -> list[Valuation]:
        return [v for v in self.items if not v.unpriceable]

    @property
    def unpriceable(self) -> list[Valuation]:
        return [v for v in self.items if v.unpriceable]

    @property
    def pricing(self) -> list[Valuation]:
        """Rows with a tier-3 query still outstanding. The bag total is a floor
        while this is non-empty — SPEC §5.3's ``≥ N div``."""
        return [v for v in self.items if v.pricing]

    @property
    def no_listings(self) -> list[Valuation]:
        """Rows whose tier-3 search ran and matched nothing. **Not** in
        :attr:`pricing`: these are finished, and a surface that keeps them spinning
        is promising a number that is not coming."""
        return [v for v in self.items if v.no_listings]

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
            "league_source": self.league_source.value if self.league_source else None,
            "league_overridden": (
                self.league_source.value != LeagueSource.CHARACTER.value
                if self.league_source
                else None
            ),
            "items": [v.to_json() for v in self.items],
            "total_chaos": round(self.total_chaos, 4),
            "total_divine": (
                round(self.total_divine, 4) if self.total_divine is not None else None
            ),
            "divine_rate": self.divine_rate,
            "priced_count": len(self.priced),
            "unpriceable_count": len(self.unpriceable),
            "unpriceable_stack": self.unpriceable_stack,
            "pricing_count": len(self.pricing),
            "no_listings_count": len(self.no_listings),
            "total_is_floor": bool(self.pricing),
            "lookups": self.lookups,
            "trade_requests": self.trade_requests,
            "exchange_requests": self.exchange_requests,
            "table": self.table.to_json() if self.table else None,
        }


class TableStatus:
    """Freshness of the bulk tables, carried with every valuation.

    :attr:`league` is **which league the loaded tables are for**, not which league
    was asked about. Those can differ — the tables are prefetched before any
    character is known — and a status that reported only "16 loaded" while the bag
    was Allflame is a status that hides the one fact that matters.
    """

    __slots__ = (
        "discovery",
        "league",
        "loaded",
        "newest",
        "note",
        "oldest",
        "requested",
        "stale",
    )

    def __init__(
        self,
        *,
        league: str | None,
        loaded: int,
        requested: int,
        oldest: datetime | None = None,
        newest: datetime | None = None,
        stale: bool = False,
        note: str | None = None,
        discovery: str | None = None,
    ) -> None:
        self.league = league
        self.loaded = loaded
        self.requested = requested
        self.oldest = oldest
        self.newest = newest
        self.stale = stale
        self.note = note
        self.discovery = discovery
        """One line about where the *list of tables* came from — the league's own
        answer or the hardcoded fallback. Separate from :attr:`note`, which is about
        the tables' contents, because "sixteen tables, all fresh" and "sixteen tables
        because nobody asked the league whether it has thirty-six" look identical from
        the outside and one of them is the bug this phase fixed."""

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
            "discovery": self.discovery,
        }


class TradeQuote:
    """A tier-3 answer: the median of the cheapest N **online** listings.

    Median rather than minimum, deliberately (SPEC §5.3). The cheapest listing on the
    official site is very often a bot, a mispriced item, or someone who logged off
    three weeks ago; pricing against it produces a number the player cannot actually
    realise.
    """

    __slots__ = (
        "attempts",
        "chaos",
        "considered",
        "listings",
        "online",
        "query",
        "query_url",
        "total",
        "unavailable",
    )

    def __init__(
        self,
        chaos: float | None,
        *,
        considered: int,
        online: int,
        total: int,
        listings: Sequence[float] = (),
        query_url: str | None = None,
        query: str = "",
        attempts: int = 1,
        unavailable: str | None = None,
    ) -> None:
        self.chaos = chaos
        self.considered = considered
        """Listings fetched. Capped at 10 — the fetch endpoint's own limit."""

        self.online = online
        self.total = total
        """What the search said the whole result set was, before any fetch.

        Read this before believing :attr:`chaos`. A ``total`` of 1 makes the "median
        of the cheapest N" a single stranger's asking price wearing a median's
        clothes — which is exactly what the first live appraisal reported as ``10.0c``
        for a jewel that had precisely one comparable in the league."""

        self.listings = list(listings)
        self.query_url = query_url
        self.query = query
        """The filters that produced this, in words: ``Amethyst Ring · rare ·
        max life ≥ 82``. A price nobody can trace back to a query is a price nobody
        can argue with, and tier 3 is the tier most likely to be wrong."""

        self.attempts = attempts
        """Searches spent. ``2`` means the first query matched nothing and the
        broadening retry ran."""

        self.unavailable = unavailable
        """Why no search happened at all, when none did. Distinct from a search that
        ran and matched nothing — see :class:`Tier3`."""

    @property
    def searched(self) -> bool:
        """Did a query actually reach the trade API?"""
        return self.unavailable is None

    def to_json(self) -> dict[str, Any]:
        return {
            "chaos": round(self.chaos, 4) if self.chaos is not None else None,
            "considered": self.considered,
            "online": self.online,
            "total": self.total,
            "listings": [round(v, 4) for v in self.listings],
            "query_url": self.query_url,
            "query": self.query,
            "attempts": self.attempts,
            "unavailable": self.unavailable,
        }


@runtime_checkable
class PricesApi(Protocol):
    """What dependents get from ``ctx.require(PricesApi)``.

    ``value`` and ``value_all`` are guaranteed **never to spend GGG account budget**:
    they read tables prefetched from poe.ninja, which is a different host with its
    own bucket. That guarantee is what lets an appraisal screen price a bag on a zone
    transition, and it is unchanged.

    What did change in Phase 4b is narrower and worth stating exactly. ``value_all``
    may now make **bulk-exchange** requests — up to a handful, on the
    ``trade-exchange-request-limit`` bucket, only for currency-class items the bulk
    index missed, and none at all when the per-league rate cache is warm. That bucket
    is ``Ip``-ruled and shares nothing with ``backend-item-request-limit``, so a
    valuation still cannot delay or starve a sync. Pass ``exchange=False`` for a pass
    that must provably make no request at all.

    Tier 3 (trade search) is still not reachable from here. ``quote`` is the only way
    in, and ``appraisal`` is the only caller that runs it eagerly — for a bag, for
    gate-passing items, bounded (SPEC §5.3).

    ## The league is an argument, not a setting

    Every pricing call takes the league the *items* are in. Resolution is
    :meth:`league_choice`: a per-call argument, then the ``prices.league`` setting
    when it is set, then the bag's own league, then :class:`LeagueUnknownError`.
    There is no final fallback, deliberately — the previous one was ``"Standard"``,
    and it priced an Allflame bag against an economy whose Divine Orb costs four
    times as much while printing a total that looked entirely reasonable.

    Because the tables are prefetched before any character is known, they may belong
    to a different league than the bag. They are then **not used**: the valuation
    comes back with everything unpriceable and a :class:`TableStatus` that says which
    league the tables are actually for. Call :meth:`ensure_tables` first — it is the
    one pricing-side method that may spend a poe.ninja request, and it costs no GGG
    budget at all.
    """

    def league_choice(
        self, bag_league: str | None = None, *, explicit: str | None = None
    ) -> LeagueChoice:
        """Resolve which league to price against, and say why. Never guesses."""
        ...

    @property
    def tables_league(self) -> str | None:
        """Which league the currently loaded tables belong to, if any."""
        ...

    @property
    def trade_requests(self) -> int:
        """Trade-search requests this process has made. A diagnostic, and the only
        honest way for a caller to report what a pass actually cost."""
        ...

    @property
    def exchange_requests(self) -> int:
        """Bulk-exchange requests this process has made."""
        ...

    async def ensure_tables(self, league: str) -> TableStatus:
        """Make the loaded tables be ``league``'s, fetching them if they are not.

        May block on poe.ninja — a surface should say what it is doing rather than
        appear to hang. Costs zero GGG rate-limit budget.
        """
        ...

    async def value(
        self,
        item: NormalizedItem,
        *,
        league: str | None = None,
        override: str | None = None,
        exchange: bool | None = None,
    ) -> Valuation:
        """Price one item: note, bulk index, bulk exchange, then ``unpriceable``."""
        ...

    async def value_all(
        self,
        items: Sequence[NormalizedItem],
        *,
        league: str | None = None,
        override: str | None = None,
        exchange: bool | None = None,
    ) -> BagValuation:
        """Price a whole bag, deduplicated, with stack-aware totals.

        ``league`` is the league the items are **in** — normally ``ItemSet.league``.
        ``override`` is a per-call instruction to price against a different economy
        anyway; it outranks both ``league`` and the ``prices.league`` setting, and
        the resulting :attr:`BagValuation.league_source` records that it did.

        ``exchange`` turns the tier-1b bulk-exchange fallback on or off for this
        call; ``None`` follows the ``exchange_fallback`` setting.
        """
        ...

    async def quote_many(
        self,
        items: Sequence[NormalizedItem],
        *,
        league: str | None = None,
        sample: int = 0,
        timeout: float | None = None,
        focus: Mapping[str, Sequence[ModFocus]] | None = None,
    ) -> Mapping[str, TradeQuote]:
        """Tier 3 for several items at once, keyed by ``uid``.

        Bounded and interruptible: whatever has answered by ``timeout`` is returned
        and the rest are abandoned, so a slow query can delay a number but never the
        output.

        **Absence means "still running"**, and only that. A query that ran and matched
        nothing is *present* with ``chaos is None``; one that could not run is present
        with :attr:`TradeQuote.unavailable` set. The caller needs those three cases
        apart to choose between ``pricing…``, "no matching listings" and "could not
        ask" — collapsing them into absence is what made the first live appraisal
        render ``pricing…`` forever next to two finished searches.

        ``focus`` is the caller's per-uid answer to *which mods matter*, and is what
        keeps a six-mod rare from becoming a near-exact-match query. See
        :class:`ModFocus`.

        The caller decides whether running this eagerly is appropriate. It is, for a
        bag of three to five rares; it is not, for a stash (SPEC §5.3).
        """
        ...

    async def bulk(self, category: str) -> Mapping[str, Price]:
        """One bulk table as ``name -> Price``. Fetches it if it is not loaded."""
        ...

    async def refresh(self, *, force: bool = False, league: str | None = None) -> TableStatus:
        """Refresh the bulk tables with conditional requests. Costs no GGG budget."""
        ...

    def status(self, league: str | None = None) -> TableStatus:
        """Table freshness, without fetching anything.

        Pass ``league`` to ask "are the loaded tables usable for *this* league?" —
        a mismatch answers ``loaded=0`` with a note, never a plausible-looking count.
        """
        ...

    async def quote(
        self,
        item: NormalizedItem,
        *,
        sample: int = 0,
        league: str | None = None,
        spec: QuerySpec | Sequence[ModFocus] | None = None,
    ) -> TradeQuote:
        """Tier 3. **On demand only** — never called from a valuation pass.

        ``sample`` is how many of the cheapest online listings to take the median of;
        ``0`` uses the configured default. Capped at 10 by the fetch endpoint.

        ``spec`` is the query: which mods to filter on, how tightly, and how many
        affix slots must still be free. Without it the query falls back to the item's
        most significant few mods and broadens on an empty result, which is a guess —
        and Phase 9 exists because that guess was wrong in both directions.
        """
        ...
