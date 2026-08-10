"""The `prices` module — PoEDex's first **feature** module.

Feature, not core, because everything below is an opinion (IMPLEMENTATION-PLAN §1.3):
which source, which league, note-before-market, median-of-cheapest-N over minimum,
how stale a table may be, and what qualifies as `unpriceable`. A core module holding
those is how a contained core stops being contained — and so, by the same rule, no
core module may depend on this one. That is enforced statically and at assembly time
by ``tests/test_boundaries.py``, which until this phase had nothing real to enforce
it against.

Three behaviours here exist to protect somebody other than the caller:

* **The tables are prefetched once at start and never on a user action.** poe.ninja
  serves ``max-age=1800`` and refreshes about every fifteen minutes; a fetch on every
  panel open would spend a community resource for identical bytes.
* **A valuation pass cannot make a request.** :meth:`value` and :meth:`value_all`
  read :class:`PriceIndex`, which has no network handle at all. Tier 3 lives behind
  :meth:`quote` and counts its own requests so a test can prove the separation.
* **poe.ninja costs zero GGG budget.** It is a different host, and `net` gives
  foreign hosts a bucket keyed by hostname. Pricing a bag cannot delay a sync.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, ClassVar

from modules.net.backend.api import NetApi, NetError, RateLimited
from modules.poeapi.backend.api import NormalizedItem, PoeApi, Source
from modules.prices.backend.api import (
    PRICES_UPDATED,
    BagValuation,
    Price,
    PricesApi,
    PricesError,
    PriceSource,
    TableStatus,
    TradeQuote,
    TradeUnavailable,
    Valuation,
)
from modules.prices.backend.ninja import (
    CATALOGUE,
    DEFAULT_TTL,
    PREFETCH,
    NinjaClient,
    PriceTable,
    TableStore,
)
from modules.prices.backend.trade import MAX_FETCH_IDS, TradeClient
from modules.prices.backend.valuation import PriceIndex
from runtime.context import ModuleContext
from runtime.errors import ModuleNotStartedError
from runtime.log import get_logger

__all__ = ["MODULE", "PricesModule"]

_fallback_log = get_logger("module.prices")

DEFAULT_LEAGUE = "Standard"
DEFAULT_TRADE_SAMPLE = MAX_FETCH_IDS


class PricesModule:
    id = "prices"
    name = "Prices"
    kind = "feature"
    requires: ClassVar[list[str]] = ["net", "poeapi"]
    """``net`` as well as ``poeapi``, and the plan's §1.3 arrow diagram shows only
    ``poeapi``. The diagram is a simplification of the *data* flow; the dependency is
    real. Two of this module's three data sources — poe.ninja and the official trade
    endpoints — are not account endpoints, so ``poeapi`` neither fetches nor should
    fetch them, and plan §4 is categorical that no module may open its own socket.
    The alternative, a passthrough on ``PoeApi``, would be a hole in the one rule
    that protects the user's account."""

    provides: type | None = PricesApi

    def __init__(
        self,
        *,
        ninja: NinjaClient | None = None,
        trade: TradeClient | None = None,
        store: TableStore | None = None,
        clock: Callable[[], float] | None = None,
        prefetch: bool = True,
    ) -> None:
        # Injectable collaborators keep the tests off the network entirely, exactly
        # as NetModule does with its client.
        self._ninja = ninja
        self._trade = trade
        self._store = store
        self._clock = clock or time.time
        self._prefetch_on_start = prefetch
        self._ctx: ModuleContext | None = None
        self._net: NetApi | None = None
        self._poeapi: PoeApi | None = None
        self._tables: dict[str, PriceTable] = {}
        self._failures: dict[str, str] = {}
        self._prefetch_task: asyncio.Task[Any] | None = None

    # -- lifecycle -------------------------------------------------------------

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        self._poeapi = ctx.require(PoeApi)
        self._net = ctx.require(NetApi)
        if self._store is None:
            self._store = TableStore(ctx.storage, clock=self._clock)
        if self._ninja is None:
            self._ninja = NinjaClient(self._net)
        if self._trade is None:
            self._trade = TradeClient(self._net, ctx.storage, clock=self._clock)

        self._load_cached_tables()
        if self._prefetch_on_start:
            # Scheduled, not awaited: module start must not block on somebody else's
            # web server, and a bag valuation before the tables land is honestly
            # reported as having none rather than being made to wait.
            self._prefetch_task = asyncio.create_task(self._prefetch())
        ctx.logger.info(
            "prices ready: league=%s, %d/%d tables cached",
            self.league,
            len(self._tables),
            len(self._wanted()),
        )

    async def stop(self) -> None:
        task, self._prefetch_task = self._prefetch_task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._ctx = None
        self._net = None
        self._poeapi = None

    def methods(self) -> dict[str, Callable[..., Any]]:
        return {
            "value_bag": self.value_bag_json,
            "status": self.status_json,
            "refresh": self.refresh_json,
            "quote": self.quote_json,
        }

    def settings_schema(self) -> dict[str, Any]:
        return {
            "league": {
                "type": "str",
                "default": DEFAULT_LEAGUE,
                "label": "Price league",
                "description": (
                    "Which league's economy to price against. Must match the league "
                    "the items are actually in; Standard prices are not Allflame "
                    "prices."
                ),
            },
            "table_ttl_seconds": {
                "type": "int",
                "default": int(DEFAULT_TTL),
                "min": 300,
                "max": 86400,
                "label": "Price table lifetime",
                "description": (
                    "poe.ninja serves max-age=1800 and refreshes roughly every 15 "
                    "minutes. Lowering this below 900 spends their bandwidth for "
                    "bytes that have not changed."
                ),
            },
            "prefetch_categories": {
                "type": "list",
                "default": list(PREFETCH),
                "label": "Tables to prefetch",
                "description": (
                    "Loaded once at start. Anything not listed is still priceable "
                    "through an explicit bulk() call, at the cost of a request."
                ),
            },
            "trade_sample": {
                "type": "int",
                "default": DEFAULT_TRADE_SAMPLE,
                "min": 3,
                "max": MAX_FETCH_IDS,
                "label": "Trade sample size",
                "description": (
                    "How many of the cheapest online listings a tier-3 quote takes "
                    "the median of. The endpoint's own maximum is 10."
                ),
            },
        }

    # -- PricesApi -------------------------------------------------------------

    @property
    def league(self) -> str:
        return str(self._setting("league", DEFAULT_LEAGUE)) or DEFAULT_LEAGUE

    def index(self) -> PriceIndex:
        return PriceIndex(tables=dict(self._tables), league=self.league)

    async def value(self, item: NormalizedItem) -> Valuation:
        return self.index().value(item)

    async def value_all(self, items: Sequence[NormalizedItem]) -> BagValuation:
        return self.index().value_all(items, table_status=self.status())

    async def bulk(self, category: str) -> Mapping[str, Price]:
        """One table as ``name -> Price``, fetching it if it is not already loaded.

        The only path in this module that may fetch outside a refresh, and it is only
        reachable when a caller names a category explicitly.
        """
        if category not in CATALOGUE:
            raise PricesError(
                f"unknown price category {category!r}; known: {', '.join(sorted(CATALOGUE))}"
            )
        table = self._tables.get(category)
        if table is None or self._is_expired(table):
            await self._refresh_one(CATALOGUE[category])
            table = self._tables.get(category)
        if table is None:
            return {}
        return {
            line.name: Price(
                line.chaos,
                PriceSource.BULK,
                category=line.category,
                detail=line.detail() or None,
                listing_count=line.listing_count or None,
                as_of=datetime.fromtimestamp(table.fetched_at, tz=UTC),
            )
            for line in table.lines
        }

    async def refresh(self, *, force: bool = False) -> TableStatus:
        """Conditional-GET every wanted table. Costs zero GGG budget."""
        changed = 0
        for category in self._wanted():
            table = self._tables.get(category.key)
            if not force and table is not None and not self._is_expired(table):
                continue
            if await self._refresh_one(category, force=force):
                changed += 1
        status = self.status()
        await self._announce(status, changed)
        return status

    def status(self) -> TableStatus:
        stamps = [table.fetched_at for table in self._tables.values()]
        wanted = self._wanted()
        note = None
        if self._failures:
            note = "; ".join(f"{key}: {reason}" for key, reason in sorted(self._failures.items()))
        elif not self._tables:
            note = "no price tables loaded yet"
        oldest = min(stamps) if stamps else None
        return TableStatus(
            league=self.league,
            loaded=len(self._tables),
            requested=len(wanted),
            oldest=_as_datetime(oldest),
            newest=_as_datetime(max(stamps) if stamps else None),
            stale=bool(oldest is not None and (self.now() - oldest) > self._ttl() * 2),
            note=note,
        )

    async def quote(self, item: NormalizedItem, *, sample: int = 0) -> TradeQuote:
        """Tier 3. On demand only — never reached from a valuation pass."""
        trade = self._require_trade()
        index = self.index()
        try:
            return await trade.quote(
                item,
                self.league,
                chaos_of=index.chaos_for_trade_id,
                sample=sample or int(self._setting("trade_sample", DEFAULT_TRADE_SAMPLE)),
            )
        except RateLimited as exc:
            raise TradeUnavailable(
                f"the trade API is rate limited; retry in {exc.retry_after:.0f}s"
            ) from None
        except NetError as exc:
            raise TradeUnavailable(f"the trade query failed: {exc}") from None

    @property
    def trade_requests(self) -> int:
        """How many trade requests have been made in this process."""
        return self._trade.requests if self._trade is not None else 0

    # -- JSON wrappers for the method registry ---------------------------------

    async def value_bag_json(self, character: str | None = None) -> dict[str, Any]:
        bag = await self._require_poeapi().get_items(character)
        result = await self.value_all(bag.by_source(Source.BAG))
        return result.to_json()

    async def status_json(self) -> dict[str, Any]:
        return self.status().to_json()

    async def refresh_json(self, force: bool = False) -> dict[str, Any]:
        return (await self.refresh(force=force)).to_json()

    async def quote_json(self, uid: str, character: str | None = None) -> dict[str, Any]:
        """Quote one item of the current bag, by uid.

        Takes a uid rather than an item object so the frontend cannot hand the trade
        API an item it invented.
        """
        bag = await self._require_poeapi().get_items(character)
        for item in bag.items:
            if item.uid == uid:
                return (await self.quote(item)).to_json()
        raise PricesError(f"no item {uid!r} in the current bag")

    # -- internals -------------------------------------------------------------

    def now(self) -> float:
        return self._clock()

    def _ttl(self) -> float:
        return float(self._setting("table_ttl_seconds", DEFAULT_TTL))

    def _is_expired(self, table: PriceTable) -> bool:
        return table.age(self.now()) >= self._ttl()

    def _wanted(self):
        configured = self._setting("prefetch_categories", list(PREFETCH))
        keys = [str(key) for key in configured] if isinstance(configured, list) else list(PREFETCH)
        return [CATALOGUE[key] for key in keys if key in CATALOGUE]

    def _load_cached_tables(self) -> None:
        """Warm start. A table on disk is usable even when it is past its TTL —
        stale prices with an honest timestamp beat an empty panel."""
        store = self._require_store()
        league = self.league
        for category in self._wanted():
            table = store.load(league, category.key)
            if table is not None:
                self._tables[category.key] = table

    async def _prefetch(self) -> None:
        try:
            await self.refresh()
        except asyncio.CancelledError:  # pragma: no cover - shutdown race
            raise
        except Exception as exc:  # a price table is never worth failing a start over
            self._log().warning("price prefetch failed: %s", exc)

    async def _refresh_one(self, category, *, force: bool = False) -> bool:
        """Fetch one table. Returns whether its contents changed.

        A failure is recorded against the category and swallowed: one 500 from
        poe.ninja must not cost the other fifteen tables.
        """
        ninja = self._require_ninja()
        store = self._require_store()
        existing = self._tables.get(category.key)
        etag = None if force else (existing.etag if existing else None)
        try:
            table = await ninja.fetch(category, self.league, etag=etag, now=self.now())
        except (NetError, PricesError) as exc:
            self._failures[category.key] = _short(exc)
            self._log().info("price table %s unavailable: %s", category.key, exc)
            return False
        self._failures.pop(category.key, None)
        if table is None:
            # 304. Re-stamp the copy we hold so its age reflects the check, not the
            # last time the bytes changed.
            if existing is not None:
                existing.fetched_at = self.now()
                store.save(existing)
            return False
        if not table.lines:
            self._failures[category.key] = "the table came back empty"
            return False
        self._tables[category.key] = table
        store.save(table)
        return True

    async def _announce(self, status: TableStatus, changed: int) -> None:
        if self._ctx is None:
            return
        await self._ctx.events.emit(
            PRICES_UPDATED,
            {
                "league": status.league,
                "loaded": status.loaded,
                "requested": status.requested,
                "changed": changed,
                "newest": status.newest.isoformat() if status.newest else None,
            },
            source=self.id,
        )

    def _setting(self, key: str, default: Any) -> Any:
        if self._ctx is None:
            return default
        return self._ctx.settings.get(key, default)

    def _log(self) -> Any:
        return self._ctx.logger if self._ctx else _fallback_log

    def _require_poeapi(self) -> PoeApi:
        if self._poeapi is None:
            raise ModuleNotStartedError("prices has not been started")
        return self._poeapi

    def _require_ninja(self) -> NinjaClient:
        if self._ninja is None:
            raise ModuleNotStartedError("prices has not been started")
        return self._ninja

    def _require_store(self) -> TableStore:
        if self._store is None:
            raise ModuleNotStartedError("prices has not been started")
        return self._store

    def _require_trade(self) -> TradeClient:
        if self._trade is None:
            raise ModuleNotStartedError("prices has not been started")
        return self._trade

    def __repr__(self) -> str:
        return f"PricesModule(tables={len(self._tables)}, league={self.league!r})"


def _as_datetime(epoch: float | None) -> datetime | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, tz=UTC)


def _short(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text if len(text) <= 120 else text[:117] + "…"


MODULE = PricesModule()
