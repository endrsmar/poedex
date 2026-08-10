"""The `prices` module — PoEDex's first **feature** module.

Feature, not core, because everything below is an opinion (IMPLEMENTATION-PLAN §1.3):
which source, which league, note-before-market, median-of-cheapest-N over minimum,
how stale a table may be, and what qualifies as `unpriceable`. A core module holding
those is how a contained core stops being contained — and so, by the same rule, no
core module may depend on this one. That is enforced statically and at assembly time
by ``tests/test_boundaries.py``, which until this phase had nothing real to enforce
it against.

Four behaviours here exist to protect somebody other than the caller:

* **The tables are prefetched once at start and never on a user action.** poe.ninja
  serves ``max-age=1800`` and refreshes about every fifteen minutes; a fetch on every
  panel open would spend a community resource for identical bytes.
* **A valuation pass cannot spend the account's budget.** :class:`PriceIndex` has no
  network handle at all, and the one request a valuation may now make — the tier-1b
  bulk exchange, for currency the index does not carry — goes to
  ``trade-exchange-request-limit``, which is ``Ip``-ruled and shares nothing with
  ``backend-item-request-limit``. Pricing a bag still cannot delay a sync.
* **poe.ninja costs zero GGG budget.** It is a different host, and `net` gives
  foreign hosts a bucket keyed by hostname.
* **Which tables exist is a question, not a constant.** :mod:`.discovery` asks the
  league. The list used to be twenty-six names typed in by hand, and it did not
  include ``Ducat`` — so for a whole league, every ducat in a bag came back
  ``unpriceable`` while poe.ninja had eleven priced lines for them the entire time.
  A hardcoded catalogue cannot be kept correct across leagues by being careful.

## Which league a bag is priced against

This module used to answer that from its own setting, whose default was
``"Standard"``, and never looked at the bag. A character in Allflame was therefore
priced against Standard, silently: the divine rate was 897.7c instead of 209.0c
(research-notes, Phase 3), league-specific items were missing from the wrong index
and came back ``unpriceable``, and the totals looked completely ordinary. Wrong
numbers with a confident face are worse than no numbers.

So the league is now resolved per call, in this order:

1. an explicit argument (``poedex value --league``),
2. the ``prices.league`` setting **if it is set** — it defaults to empty and exists
   only to deliberately price against a different economy,
3. the bag's own league, off :attr:`ItemSet.league`,
4. :class:`LeagueUnknownError`. There is no fifth step.

The setting sits above the bag rather than below it because that is the only reading
under which it is an *override*: something you set when you want the other answer,
that then wins. Empty by default means the ordinary path never consults it at all.

The tables are prefetched before any character is known, so they may be a different
league's. They are never quietly reused for another: :meth:`status` reports which
league the loaded tables are for, a mismatch prices nothing, and
:meth:`ensure_tables` is the explicit "go and get the right ones" that a surface
calls — announcing it, because thirty-odd conditional requests take a moment.
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
    LeagueChoice,
    LeagueSource,
    LeagueUnknownError,
    ModFocus,
    Price,
    PricesApi,
    PricesError,
    PriceSource,
    TableStatus,
    TradeQuote,
    TradeUnavailable,
    Valuation,
)
from modules.prices.backend.discovery import (
    DISCOVERY_TTL,
    PROBED,
    STATIC,
    CatalogueStore,
    LeagueCatalogue,
    candidates_from_slugs,
)
from modules.prices.backend.exchange import (
    EXCHANGE_MAX_WANTS,
    RATES_TTL,
    ExchangeClient,
)
from modules.prices.backend.ninja import (
    CANDIDATES,
    CATALOGUE,
    DEFAULT_TTL,
    NEVER_PREFETCH,
    PREFETCH,
    NinjaCategory,
    NinjaClient,
    PriceTable,
    TableStore,
)
from modules.prices.backend.trade import MAX_FETCH_IDS, TradeClient
from modules.prices.backend.valuation import PriceIndex, exchangeable
from runtime.context import ModuleContext
from runtime.errors import ModuleNotStartedError
from runtime.log import get_logger

__all__ = ["MODULE", "PricesModule"]

_fallback_log = get_logger("module.prices")

NO_OVERRIDE = ""
"""The ``league`` setting's default. Empty means "follow the bag".

It was ``"Standard"``, and that constant is the whole bug: a default that is
plausible for most accounts, invisible when wrong, and attached to the number every
other number in the tool is denominated in.
"""

DEFAULT_TRADE_SAMPLE = MAX_FETCH_IDS

# What one table fetch did. Four outcomes rather than a bool, because discovery has
# to tell "this league has no incubators" from "poe.ninja gave me a 500".
_CHANGED = "changed"
_UNCHANGED = "unchanged"
_EMPTY = "empty"
_FAILED = "failed"


def _spec(category: NinjaCategory) -> dict[str, str]:
    return {"kind": category.kind, "type": category.type, "label": category.label}


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
        exchange: ExchangeClient | None = None,
        store: TableStore | None = None,
        catalogue: CatalogueStore | None = None,
        clock: Callable[[], float] | None = None,
        prefetch: bool = True,
    ) -> None:
        # Injectable collaborators keep the tests off the network entirely, exactly
        # as NetModule does with its client.
        self._ninja = ninja
        self._trade = trade
        self._exchange = exchange
        self._store = store
        self._catalogue_store = catalogue
        self._catalogue: LeagueCatalogue | None = None
        """Which tables this league serves, from :mod:`.discovery`. ``None`` until a
        league is adopted; a record with ``source == static`` means discovery has not
        run or could not, and the static :data:`PREFETCH` is in use."""

        self._clock = clock or time.time
        self._prefetch_on_start = prefetch
        self._ctx: ModuleContext | None = None
        self._net: NetApi | None = None
        self._poeapi: PoeApi | None = None
        self._tables: dict[str, PriceTable] = {}
        self._tables_league: str | None = None
        """Which league ``_tables`` belong to. ``None`` until something establishes
        one — there is no starting guess."""

        self._failures: dict[str, str] = {}
        self._empty: set[str] = set()
        """Tables that answered 200 with nothing in them. Not a failure — this
        league genuinely has no incubators — so it must not reach the status note,
        which is for things a user might act on."""

        self._prefetch_task: asyncio.Task[Any] | None = None

    # -- lifecycle -------------------------------------------------------------

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        self._poeapi = ctx.require(PoeApi)
        self._net = ctx.require(NetApi)
        if self._store is None:
            self._store = TableStore(ctx.storage, clock=self._clock)
        if self._catalogue_store is None:
            self._catalogue_store = CatalogueStore(ctx.storage)
        if self._ninja is None:
            self._ninja = NinjaClient(self._net)
        if self._trade is None:
            self._trade = TradeClient(self._net, ctx.storage, clock=self._clock)
        if self._exchange is None:
            self._exchange = ExchangeClient(
                self._net,
                ctx.storage,
                clock=self._clock,
                rates_ttl=float(self._setting("exchange_ttl_seconds", RATES_TTL)),
            )

        # Nothing here knows which character will be appraised, so the only league
        # that can be prefetched is one the user named. Without an override the
        # module starts empty and `ensure_tables` fills it from the disk cache — free
        # — the moment a bag arrives with a league on it.
        self._tables_league = self.override
        if self._tables_league is not None:
            self._catalogue = self._catalogue_store.load(self._tables_league)
            self._load_cached_tables()
            if self._prefetch_on_start:
                # Scheduled, not awaited: module start must not block on somebody
                # else's web server, and a bag valuation before the tables land is
                # honestly reported as having none rather than being made to wait.
                self._prefetch_task = asyncio.create_task(self._prefetch())
        ctx.logger.info(
            "prices ready: league=%s, %d/%d tables cached",
            self._tables_league or "unset (follows the character's bag)",
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
            "catalogue": self.catalogue_json,
        }

    def settings_schema(self) -> dict[str, Any]:
        return {
            "league": {
                "type": "str",
                "default": NO_OVERRIDE,
                "label": "Price league override",
                "description": (
                    "Leave empty — every bag is then priced against the league its "
                    "own character is in, which is the only source that cannot be "
                    "wrong. Set it to deliberately price against a different "
                    "economy; it then wins over the character, and every surface "
                    "says so. Standard prices are not Allflame prices."
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
                "label": "Tables to prefetch (fallback)",
                "description": (
                    "Only used when discovery is off or has failed. Normally the "
                    "league itself decides which tables exist — this hardcoded list "
                    "is what missed ducats for a whole league."
                ),
            },
            "discover_categories": {
                "type": "bool",
                "default": True,
                "label": "Ask the league which tables it has",
                "description": (
                    "Probes every type poe.ninja documents, plus any category its "
                    "sitemap lists that this build has never heard of, once a day "
                    "per league. Costs no GGG budget. Turning it off restores the "
                    "hardcoded list, which is a list that goes stale every league."
                ),
            },
            "discovery_ttl_seconds": {
                "type": "int",
                "default": int(DISCOVERY_TTL),
                "min": 3600,
                "max": 2_592_000,
                "label": "Table discovery lifetime",
                "description": (
                    "Which types a league serves changes with a patch, not with the "
                    "hour. A day is generous."
                ),
            },
            "exchange_fallback": {
                "type": "bool",
                "default": True,
                "label": "Bulk-exchange fallback",
                "description": (
                    "For currency and fragments poe.ninja does not index at all, ask "
                    "GGG's bulk exchange. A handful of requests on the trade IP "
                    "bucket, cached per league; never touches the account's budget."
                ),
            },
            "exchange_ttl_seconds": {
                "type": "int",
                "default": int(RATES_TTL),
                "min": 900,
                "max": 604800,
                "label": "Bulk-exchange rate lifetime",
                "description": (
                    "trade-exchange-request-limit allows 30 requests per half hour "
                    "for the whole IP, shared with your browser. These are thin "
                    "markets; six hours is not stale."
                ),
            },
            "exchange_batch": {
                "type": "int",
                "default": EXCHANGE_MAX_WANTS,
                "min": 1,
                "max": EXCHANGE_MAX_WANTS,
                "label": "Bulk-exchange batch size",
                "description": (
                    "How many currencies to ask about in one request. The endpoint "
                    "caps at 10 and returns only the 100 cheapest rows across the "
                    "whole batch, so a starved currency is re-queried alone rather "
                    "than priced off two offers at the floor."
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
            "broaden_on_no_matches": {
                "type": "bool",
                "default": True,
                "label": "Retry a tier-3 search that matched nothing",
                "description": (
                    "When the first query finds no listings, spend one more search "
                    "on a wider version of it — one filter, no roll floor. Costs at "
                    "most one extra request per item, and only for items that would "
                    "otherwise have no answer at all."
                ),
            },
        }

    # -- PricesApi -------------------------------------------------------------

    @property
    def override(self) -> str | None:
        """The ``prices.league`` setting, or ``None`` when it is not set."""
        return str(self._setting("league", NO_OVERRIDE)).strip() or None

    @property
    def tables_league(self) -> str | None:
        return self._tables_league

    def league_choice(
        self, bag_league: str | None = None, *, explicit: str | None = None
    ) -> LeagueChoice:
        """Argument, then override, then the bag. No fourth answer.

        Raising here rather than returning a default is the entire fix. The caller
        that has nothing to offer is a caller that does not know which economy it is
        talking about, and every number it would go on to produce would be wrong by
        whatever the two leagues' divine rates differ by.
        """
        if explicit and explicit.strip():
            return LeagueChoice(explicit.strip(), LeagueSource.ARGUMENT)
        override = self.override
        if override:
            return LeagueChoice(override, LeagueSource.SETTING)
        if bag_league and bag_league.strip():
            return LeagueChoice(bag_league.strip(), LeagueSource.CHARACTER)
        raise LeagueUnknownError(
            "cannot tell which league to price against: these items carry no league "
            "and no override is set. Pass --league, or set the prices.league "
            "setting. Refusing rather than assuming Standard — a Divine Orb was "
            "897.7c there and 209.0c in Allflame on the same day."
        )

    def index(self, league: str | None = None) -> PriceIndex:
        """The loaded tables as an index — but only for the league they belong to.

        Ask for another league and the index comes back **empty** rather than
        answering out of the wrong economy's tables. Every item is then reported
        ``unpriceable`` with a table status that names both leagues, which is the
        loud version of the failure this module used to have quietly.
        """
        target = league or self._tables_league
        if target is None:
            raise LeagueUnknownError(
                "no price tables have been loaded for any league yet"
            )
        if target != self._tables_league:
            return PriceIndex(tables={}, league=target)
        return PriceIndex(tables=dict(self._tables), league=target)

    async def value(
        self,
        item: NormalizedItem,
        *,
        league: str | None = None,
        override: str | None = None,
        exchange: bool | None = None,
    ) -> Valuation:
        return (
            await self.value_all(
                [item], league=league, override=override, exchange=exchange
            )
        ).items[0]

    async def value_all(
        self,
        items: Sequence[NormalizedItem],
        *,
        league: str | None = None,
        override: str | None = None,
        exchange: bool | None = None,
    ) -> BagValuation:
        choice = self.league_choice(league, explicit=override)
        index = self.index(choice.league)
        spent = 0
        if self._use_exchange(exchange):
            spent = await self._fill_exchange_rates(index, items, choice.league)
        return index.value_all(
            items,
            table_status=self.status(choice.league),
            league_source=choice.source,
            exchange_requests=spent,
        )

    def _use_exchange(self, explicit: bool | None) -> bool:
        if explicit is not None:
            return bool(explicit)
        return bool(self._setting("exchange_fallback", True))

    async def _fill_exchange_rates(
        self, index: PriceIndex, items: Sequence[NormalizedItem], league: str
    ) -> int:
        """Tier 1b. Ask the bulk exchange about what tier 1 could not answer.

        Two filters before a single request goes out, both cheap and both load
        bearing. Only items the bulk index *missed* — an exchange query for something
        poe.ninja already priced is a request spent to learn nothing. And only items
        the exchange could plausibly trade (:func:`exchangeable`): it deals in
        stackable currency, and sending it a rare would be tier 3's job done badly.
        """
        client = self._exchange
        if client is None:
            return 0
        wanted: list[str] = []
        seen: set[str] = set()
        for item in items:
            if not exchangeable(item) or not item.name:
                continue
            if index.market_line(item) is not None:
                continue
            if item.name not in seen:
                seen.add(item.name)
                wanted.append(item.name)
        index.exchange_attempted = True
        if not wanted:
            return 0
        before = client.requests
        try:
            rates = await client.rates(
                wanted,
                league,
                sample=int(self._setting("trade_sample", DEFAULT_TRADE_SAMPLE)),
                batch=int(self._setting("exchange_batch", EXCHANGE_MAX_WANTS)),
            )
        except RateLimited as exc:
            self._log().info(
                "bulk exchange refused for %d name(s); retry in %.0fs",
                len(wanted),
                exc.retry_after,
            )
            return 0
        except (NetError, PricesError) as exc:
            self._log().info("bulk exchange unavailable: %s", exc)
            return client.requests - before
        index.exchange_rates = {
            name: Price(
                rate.chaos,
                PriceSource.EXCHANGE,
                category="exchange",
                detail=(
                    f"{rate.offers} live offer(s)"
                    + (", cheapest-end sample" if rate.truncated else "")
                ),
                listing_count=rate.total or None,
                sample_size=rate.offers or None,
                as_of=_as_datetime(client.cached(league).fetched_at or None),
            )
            for name, rate in rates.items()
            if rate.chaos is not None and rate.chaos > 0
        }
        return client.requests - before

    async def ensure_tables(self, league: str) -> TableStatus:
        """Load ``league``'s tables: from disk if they are there, else from poe.ninja.

        Separate from :meth:`value_all` so the no-request guarantee survives. A
        surface calls this once, having said what it is doing, and then values as
        many bags as it likes without touching a socket.
        """
        target = (league or "").strip()
        if not target:
            raise LeagueUnknownError("ensure_tables needs a league")
        # A CLI run reaches this within milliseconds of start, with the background
        # prefetch still in flight. Two concurrent passes would fetch every table
        # twice — poe.ninja's bandwidth, spent on bytes we are already downloading.
        await self._settle_prefetch()
        if target != self._tables_league:
            self._adopt_league(target)
        missing = [
            category
            for category in self._wanted()
            if category.key not in self._tables or self._is_expired(self._tables[category.key])
        ]
        if missing:
            await self.refresh()
        return self.status()

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
            self._require_ninja()  # "not started" beats "no league" as a diagnosis
            await self._refresh_one(CATALOGUE[category], self._league_or_raise())
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

    async def refresh(self, *, force: bool = False, league: str | None = None) -> TableStatus:
        """Conditional-GET every wanted table. Costs zero GGG budget.

        Discovery runs first when its record has expired, because "which tables"
        has to be answered before "are they fresh". The probe *is* a fetch, so the
        two passes do not stack: a discovery refresh leaves every served table
        loaded, and the loop below then finds nothing to do.
        """
        target = (league or "").strip() or self._league_or_raise()
        if target != self._tables_league:
            self._adopt_league(target)
        changed, touched = await self._discover(target, force=force)
        for category in self._wanted():
            if category.key in touched:
                # Discovery just fetched it. Re-fetching under `force` would double
                # every request in the one pass that is already the most expensive.
                continue
            table = self._tables.get(category.key)
            if not force and table is not None and not self._is_expired(table):
                continue
            if await self._refresh_one(category, target, force=force) == _CHANGED:
                changed += 1
        status = self.status()
        await self._announce(status, changed)
        return status

    async def _discover(
        self, league: str, *, force: bool = False
    ) -> tuple[int, set[str]]:
        """Ask ``league`` which tables it serves.

        Returns ``(tables changed, keys this pass fetched)``. The second half is what
        stops a forced refresh paying for every table twice: the probe *is* a fetch.

        Never raises. Every failure path ends with the static catalogue in use and a
        record that says ``static``, because a league whose type list we could not
        establish is still a league whose currency we can price.
        """
        if not bool(self._setting("discover_categories", True)):
            return 0, set()
        record = self._catalogue
        if not force and record is not None and record.fresh(self.now(), self._discovery_ttl()):
            return 0, set()

        self._require_ninja()  # "not started" beats a confusing sitemap failure
        extra, unmapped = await self._sitemap_candidates(league)
        probes: list[NinjaCategory] = [CATALOGUE[key] for key in CANDIDATES] + extra

        served: list[str] = []
        empty: list[str] = []
        failed: dict[str, str] = {}
        found: dict[str, dict[str, str]] = {}
        changed = 0
        touched: set[str] = set()
        for category in probes:
            touched.add(category.key)
            outcome = await self._refresh_one(category, league, force=force)
            if outcome == _CHANGED:
                changed += 1
            if outcome == _FAILED:
                failed[category.key] = self._failures.get(category.key, "unavailable")
                if category.key not in CATALOGUE and category.kind == "exchange":
                    # A type nobody has typed in has an unknown response shape; the
                    # exchange path 404s for an item overview. One retry, then it
                    # goes on the unmapped list rather than being guessed at forever.
                    retry = NinjaCategory(
                        key=category.key,
                        kind="item",
                        type=category.type,
                        label=category.label,
                    )
                    if await self._refresh_one(retry, league, force=force) == _CHANGED:
                        changed += 1
                        failed.pop(category.key, None)
                        served.append(category.key)
                        found[category.key] = _spec(retry)
                        continue
                    unmapped.append(category.type)
                continue
            if outcome == _EMPTY:
                empty.append(category.key)
                continue
            served.append(category.key)
            if category.key not in CATALOGUE:
                found[category.key] = _spec(category)

        if not served:
            # Nothing answered — poe.ninja is down, or the league name is wrong.
            # Recording "this league serves no tables" would be a lie that survives
            # for a day, so nothing is recorded at all.
            self._log().warning(
                "table discovery for %s found nothing; using the built-in list", league
            )
            self._catalogue = LeagueCatalogue(
                league=league, discovered_at=self.now(), source=STATIC,
                served=[c.key for c in self._static_wanted()],
            )
            return changed, touched

        record = LeagueCatalogue(
            league=league,
            discovered_at=self.now(),
            served=served,
            empty=empty,
            failed=failed,
            found=found,
            unmapped=sorted(set(unmapped)),
            source=PROBED,
        )
        self._catalogue = record
        self._require_catalogue_store().save(record)
        self._log().info("table discovery: %s", record.describe())
        if found:
            self._log().warning(
                "poe.ninja serves %d table(s) this build has never heard of: %s — "
                "consider adding them to CATALOGUE",
                len(found),
                ", ".join(sorted(found)),
            )
        return changed, touched

    async def _sitemap_candidates(
        self, league: str
    ) -> tuple[list[NinjaCategory], list[str]]:
        """Categories the sitemap knows about that :data:`CATALOGUE` does not."""
        try:
            slugs = await self._require_ninja().sitemap_slugs(league)
        except (NetError, PricesError) as exc:
            self._log().info("no sitemap for %s: %s", league, exc)
            return [], []
        if not slugs:
            self._log().info("the sitemap lists no categories for %s", league)
            return [], []
        extra, unmapped = candidates_from_slugs(slugs)
        return [c for c in extra if c.key not in NEVER_PREFETCH], unmapped

    def status(self, league: str | None = None) -> TableStatus:
        wanted = self._wanted()
        target = (league or "").strip() or None
        if target is not None and target != self._tables_league:
            # The one thing this must not do is report a page of healthy tables while
            # answering about a league none of them describes.
            held = self._tables_league or "no league"
            return TableStatus(
                league=self._tables_league,
                loaded=0,
                requested=len(wanted),
                stale=True,
                note=(
                    f"the loaded price tables are {held}'s, not {target}'s — nothing "
                    f"here can be priced against {target} until they are refetched"
                ),
                discovery=self._discovery_note(),
            )
        stamps = [table.fetched_at for table in self._tables.values()]
        note = None
        # Only failures on tables we actually want. Discovery probes types this
        # league does not serve, and a 404 from one of those is the probe working —
        # putting it in a status note would teach the user to ignore status notes.
        keys = {category.key for category in wanted}
        failures = {key: reason for key, reason in self._failures.items() if key in keys}
        if failures:
            note = "; ".join(f"{key}: {reason}" for key, reason in sorted(failures.items()))
        elif not self._tables:
            note = "no price tables loaded yet"
        oldest = min(stamps) if stamps else None
        return TableStatus(
            league=self._tables_league,
            loaded=len(self._tables),
            requested=len(wanted),
            oldest=_as_datetime(oldest),
            newest=_as_datetime(max(stamps) if stamps else None),
            stale=bool(oldest is not None and (self.now() - oldest) > self._ttl() * 2),
            note=note,
            discovery=self._discovery_note(),
        )

    def _discovery_note(self) -> str:
        record = self._catalogue
        if record is None:
            if not bool(self._setting("discover_categories", True)):
                return (
                    f"built-in list of {len(self._static_wanted())} table(s) — "
                    "discovery is switched off"
                )
            return "not asked yet"
        return record.describe()

    def catalogue(self, league: str | None = None) -> LeagueCatalogue | None:
        """The discovery record for a league, without fetching anything."""
        target = (league or "").strip() or self._tables_league
        if target is None:
            return None
        if self._catalogue is not None and self._catalogue.league == target:
            return self._catalogue
        store = self._catalogue_store
        return store.load(target) if store is not None else None

    async def quote(
        self,
        item: NormalizedItem,
        *,
        sample: int = 0,
        league: str | None = None,
        focus: Sequence[ModFocus] | None = None,
    ) -> TradeQuote:
        """Tier 3. On demand only — never reached from a valuation pass."""
        trade = self._require_trade()
        choice = self.league_choice(league)
        index = self.index(choice.league)
        try:
            return await trade.quote(
                item,
                choice.league,
                chaos_of=index.chaos_for_trade_id,
                sample=sample or int(self._setting("trade_sample", DEFAULT_TRADE_SAMPLE)),
                focus=focus,
                retry_on_empty=bool(self._setting("broaden_on_no_matches", True)),
            )
        except RateLimited as exc:
            raise TradeUnavailable(
                f"the trade API is rate limited; retry in {exc.retry_after:.0f}s"
            ) from None
        except NetError as exc:
            raise TradeUnavailable(f"the trade query failed: {exc}") from None

    async def quote_many(
        self,
        items: Sequence[NormalizedItem],
        *,
        league: str | None = None,
        sample: int = 0,
        timeout: float | None = None,
        focus: Mapping[str, Sequence[ModFocus]] | None = None,
    ) -> dict[str, TradeQuote]:
        """Tier 3 for several items, bounded by ``timeout``. Keyed by ``uid``.

        Concurrency here is honesty, not speed. `net` serialises every request
        through one lock and one limiter, so the queries still go out one at a time
        and the shared budget still sees each of them — what running them as tasks
        buys is the ability to **stop waiting**. SPEC §5.3 asks that a tier-3 query
        never gate the grid; a timeout the caller controls is how that is kept when
        one search is slow and four are not.

        **Absence means "still running", and nothing else.** A search that ran and
        matched nothing is present with ``chaos is None``; a query that could not be
        made is present with :attr:`TradeQuote.unavailable` set. That distinction is
        the whole of the fix for ``pricing…`` rendering forever next to two searches
        that had already come back empty — a caller cannot report an answer it was
        handed as an absence.

        An item that could not be priced keeps whatever it had, which is normally
        ``unpriceable`` — and never a zero.
        """
        rows = [item for item in items if item.uid]
        if not rows:
            return {}
        self._require_trade()
        results: dict[str, TradeQuote] = {}
        wanted = dict(focus or {})

        async def one(item: NormalizedItem) -> None:
            try:
                quote = await self.quote(
                    item, sample=sample, league=league, focus=wanted.get(item.uid)
                )
            except (TradeUnavailable, PricesError, LeagueUnknownError) as exc:
                self._log().info("no tier-3 quote for %s: %s", item.name, exc)
                results[item.uid] = TradeQuote(
                    None, considered=0, online=0, total=0, attempts=0, unavailable=str(exc)
                )
                return
            results[item.uid] = quote

        tasks = [asyncio.create_task(one(item)) for item in rows]
        try:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
        except asyncio.CancelledError:  # pragma: no cover - shutdown race
            for task in tasks:
                task.cancel()
            raise
        for task in pending:
            task.cancel()
        if pending:
            self._log().info(
                "%d tier-3 quote(s) still running after %.0fs; leaving them pending",
                len(pending),
                timeout or 0.0,
            )
            # Cancellation is requested, not awaited: the point of the timeout is to
            # return now. `net` counted the requests already, so nothing is hidden.
            for task in pending:
                with suppress(asyncio.CancelledError, Exception):
                    await task
        for task in done:
            with suppress(Exception):
                task.result()
        return results

    @property
    def trade_requests(self) -> int:
        """How many trade requests have been made in this process."""
        return self._trade.requests if self._trade is not None else 0

    @property
    def exchange_requests(self) -> int:
        """How many bulk-exchange requests have been made in this process."""
        return self._exchange.requests if self._exchange is not None else 0

    # -- JSON wrappers for the method registry ---------------------------------

    async def value_bag_json(self, character: str | None = None) -> dict[str, Any]:
        bag = await self._require_poeapi().get_items(character)
        choice = self.league_choice(bag.league)
        await self.ensure_tables(choice.league)
        result = await self.value_all(bag.by_source(Source.BAG), league=bag.league)
        payload = result.to_json()
        payload["character"] = bag.character
        return payload

    async def status_json(self, league: str | None = None) -> dict[str, Any]:
        return self.status(league).to_json()

    async def refresh_json(
        self, force: bool = False, league: str | None = None
    ) -> dict[str, Any]:
        return (await self.refresh(force=force, league=league)).to_json()

    async def catalogue_json(self, league: str | None = None) -> dict[str, Any]:
        """What discovery believes about a league's tables. Fetches nothing."""
        record = self.catalogue(league)
        if record is None:
            return {
                "league": (league or "").strip() or self._tables_league,
                "source": STATIC,
                "served": [c.key for c in self._static_wanted()],
                "note": self._discovery_note(),
            }
        payload = record.to_json()
        payload["note"] = record.describe()
        return payload

    async def quote_json(self, uid: str, character: str | None = None) -> dict[str, Any]:
        """Quote one item of the current bag, by uid.

        Takes a uid rather than an item object so the frontend cannot hand the trade
        API an item it invented.
        """
        bag = await self._require_poeapi().get_items(character)
        for item in bag.items:
            if item.uid == uid:
                return (await self.quote(item, league=bag.league)).to_json()
        raise PricesError(f"no item {uid!r} in the current bag")

    # -- internals -------------------------------------------------------------

    def now(self) -> float:
        return self._clock()

    def _ttl(self) -> float:
        return float(self._setting("table_ttl_seconds", DEFAULT_TTL))

    def _is_expired(self, table: PriceTable) -> bool:
        return table.age(self.now()) >= self._ttl()

    def _wanted(self) -> list[NinjaCategory]:
        """Which tables to hold for the current league.

        The league's own answer when discovery has one, and the hardcoded list only
        as a fallback. That precedence is the fix: the hardcoded list is what said
        for a whole league that ``Ducat`` did not exist.
        """
        record = self._catalogue
        if record is not None and record.fresh(self.now(), self._discovery_ttl()):
            categories = record.categories()
            if categories:
                return categories
        return self._static_wanted()

    def _static_wanted(self) -> list[NinjaCategory]:
        configured = self._setting("prefetch_categories", list(PREFETCH))
        keys = [str(key) for key in configured] if isinstance(configured, list) else list(PREFETCH)
        return [CATALOGUE[key] for key in keys if key in CATALOGUE]

    def _discovery_ttl(self) -> float:
        return float(self._setting("discovery_ttl_seconds", DISCOVERY_TTL))

    def _league_or_raise(self) -> str:
        if self._tables_league is None:
            raise LeagueUnknownError(
                "no league has been established: call ensure_tables(league) first, "
                "or set the prices.league setting"
            )
        return self._tables_league

    def _adopt_league(self, league: str) -> None:
        """Switch to another league's tables.

        The held tables are dropped rather than merged. Two leagues' overviews share
        every item name and agree on almost no price, so a merged index would answer
        confidently out of whichever league happened to be fetched last.
        """
        if self._tables_league is not None and self._tables:
            self._log().info(
                "switching price tables from %s to %s", self._tables_league, league
            )
        self._tables_league = league
        self._tables = {}
        self._failures = {}
        self._empty = set()
        # The discovery record first: it decides what `_load_cached_tables` even
        # looks for. Loading tables against the previous league's type list would
        # quietly leave this league's own mechanics out.
        store = self._catalogue_store
        self._catalogue = store.load(league) if store is not None else None
        self._load_cached_tables()

    def _load_cached_tables(self) -> None:
        """Warm start. A table on disk is usable even when it is past its TTL —
        stale prices with an honest timestamp beat an empty panel."""
        store = self._require_store()
        league = self._league_or_raise()
        for category in self._wanted():
            table = store.load(league, category.key)
            if table is not None:
                self._tables[category.key] = table

    async def _settle_prefetch(self) -> None:
        """Wait for the start-time prefetch, if one is still running."""
        task = self._prefetch_task
        if task is None or task.done():
            return
        with suppress(asyncio.CancelledError, Exception):
            await task

    async def _prefetch(self) -> None:
        try:
            await self.refresh()
        except asyncio.CancelledError:  # pragma: no cover - shutdown race
            raise
        except Exception as exc:  # a price table is never worth failing a start over
            self._log().warning("price prefetch failed: %s", exc)

    async def _refresh_one(
        self, category: NinjaCategory, league: str, *, force: bool = False
    ) -> str:
        """Fetch one table. Returns one of the four outcomes above.

        A failure is recorded against the category and swallowed: one 500 from
        poe.ninja must not cost the other thirty-seven tables.

        ``empty`` is distinguished from ``failed`` because discovery needs the
        difference. A table that answers 200 with no lines is a table this league
        does not have — skip it for a day. A table that 500s is a table we do not
        know about — ask again.
        """
        ninja = self._require_ninja()
        store = self._require_store()
        existing = self._tables.get(category.key)
        etag = None if force else (existing.etag if existing else None)
        try:
            table = await ninja.fetch(category, league, etag=etag, now=self.now())
        except (NetError, PricesError) as exc:
            self._failures[category.key] = _short(exc)
            self._log().info("price table %s unavailable: %s", category.key, exc)
            return _FAILED
        self._failures.pop(category.key, None)
        if table is None:
            # 304. Re-stamp the copy we hold so its age reflects the check, not the
            # last time the bytes changed.
            if existing is not None:
                existing.fetched_at = self.now()
                store.save(existing)
            return _UNCHANGED
        if not table.lines:
            self._empty.add(category.key)
            return _EMPTY
        self._empty.discard(category.key)
        self._tables[category.key] = table
        store.save(table)
        return _CHANGED

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

    def _require_catalogue_store(self) -> CatalogueStore:
        if self._catalogue_store is None:
            raise ModuleNotStartedError("prices has not been started")
        return self._catalogue_store

    def __repr__(self) -> str:
        return f"PricesModule(tables={len(self._tables)}, league={self._tables_league!r})"


def _as_datetime(epoch: float | None) -> datetime | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, tz=UTC)


def _short(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text if len(text) <= 120 else text[:117] + "…"


MODULE = PricesModule()
