"""The bulk exchange — a last resort for currency the bulk index does not carry.

``POST /api/trade/exchange/{league}``. Measured 2026-08-10; research-notes §11.

## Where this sits, and where it does not

**Below poe.ninja, above ``unpriceable``.** If a currency-class item misses the bulk
index, one of two things is true: poe.ninja has no table for it (which
:mod:`.discovery` now fixes properly, by asking the league what it serves), or GGG
trades something poe.ninja has never indexed at all. This file only answers the
second. It is a safety net, not the ducat fix — ducats were a discovery failure and
are priced at tier 1 again.

That ordering matters for cost. poe.ninja is free and cached for thirty minutes;
this endpoint is governed by ``trade-exchange-request-limit``, ``Ip``-ruled,
``5:15:60, 10:90:300, 30:300:1800`` — five requests in fifteen seconds and thirty in
half an hour, shared with whatever else the player's IP is doing. So: only for
misses, batched, and cached hard.

## Batching, and the measurement that constrains it

The endpoint takes an **array** of ``want`` ids — up to ten; eleven is a 400 with
``Too many items `want` items selected``. Batching is therefore obviously right and
subtly dangerous, because the response caps at **100 results sorted by price
ascending across the whole batch**. Ten ducat ids in one request returned 100 rows
that were *all* at the 1-chaos floor, and Merrick's Ducat got two of them; alone it
returns 39 offers spanning 1c to 5c and a median of 3c. The batched number was a
third of the real rate and looked exactly as confident.

The rule that makes batching safe falls out of that sort order: **if a want received
at least ``sample`` offers from a globally cheapest-first result set, those are
precisely its own cheapest ``sample``.** So a batch is trusted per want, and only
the *starved* wants — fewer than ``sample`` offers back, from a batch the server
truncated — are re-queried alone. On the ducats that is one batch plus one re-query
instead of eleven requests, with the same numbers.

## Median of the cheapest N, again

Same rule as tier 3 and for the same reason (SPEC §5.3). The two cheapest ducat
offers on the wire were 1 chaos with stock 1 — that is somebody dumping a single
unit, not a rate. The median of the cheapest ten is 3 chaos, which is what the
player can actually realise.

⚠️ Exchange listings carry the same third-party PII as trade listings:
``account.name``, ``lastCharacterName`` and a ``whisper`` string. Nothing here logs
a listing, and only the numbers survive into :class:`ExchangeRate`.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote as urlquote

from modules.net.backend.api import NetApi, NetError
from modules.prices.backend.api import PricesError
from runtime.log import get_logger
from runtime.storage import Storage

__all__ = [
    "EXCHANGE_MAX_WANTS",
    "EXCHANGE_PATH",
    "EXCHANGE_ROUTE",
    "RATES_TTL",
    "STATIC_PATH",
    "ExchangeClient",
    "ExchangeRate",
    "LeagueRates",
    "StaticIndex",
    "parse_exchange_offers",
]

_log = get_logger("module.prices.exchange")

EXCHANGE_PATH = "/api/trade/exchange"
STATIC_PATH = "/api/trade/data/static"

EXCHANGE_ROUTE = "trade:exchange"
STATIC_ROUTE = "trade:static"

EXCHANGE_MAX_WANTS = 10
"""The endpoint's own limit. Eleven is a 400 — measured, not assumed."""

RESULT_CAP = 100
"""How many rows one response carries, whatever ``total`` says. Also measured. This
is the number that makes a naive batch lie."""

RATES_TTL = 21600.0
"""Six hours. Bulk rates for the things poe.ninja does not index are thin markets
that move slowly, and ``trade-exchange-request-limit`` allows thirty requests per
half hour for the whole IP — including the player's own browser."""

STATIC_TTL = 86400.0
"""One day. ``/api/trade/data/static`` is 195 kB of name→id mapping that changes
with a patch."""

STATIC_CACHE_KEY = "trade-static.json"
RATES_CACHE_PREFIX = "exchange-rates"
CACHE_VERSION = 1


@dataclass(frozen=True, slots=True)
class ExchangeRate:
    """One currency's bulk rate, in chaos, and how solid it is."""

    trade_id: str
    chaos: float | None
    offers: int
    """Online offers the median was taken from. ``0`` means nobody is selling it,
    which is a real answer and **not** a price of zero."""

    total: int = 0
    """What the search said the whole result set was, before the 100-row cap."""

    truncated: bool = False
    """The batch this came from was capped and this want was starved. Only ever
    ``True`` on a rate the re-query budget could not reach; the surface should treat
    it as a floor."""

    def to_json(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "chaos": round(self.chaos, 4) if self.chaos is not None else None,
            "offers": self.offers,
            "total": self.total,
            "truncated": self.truncated,
        }

    @classmethod
    def from_json(cls, data: Any) -> ExchangeRate | None:
        if not isinstance(data, Mapping) or not isinstance(data.get("trade_id"), str):
            return None
        chaos = data.get("chaos")
        return cls(
            trade_id=str(data["trade_id"]),
            chaos=float(chaos) if isinstance(chaos, (int, float)) else None,
            offers=int(data.get("offers") or 0),
            total=int(data.get("total") or 0),
            truncated=bool(data.get("truncated")),
        )


@dataclass
class LeagueRates:
    """Every rate we have asked for in one league, with one timestamp.

    Per league and never shared: a Merrick's Ducat is 3c in Allflame and does not
    trade at all in Standard, and the whole reason `prices` refuses to reuse tables
    across leagues applies here identically.
    """

    league: str
    fetched_at: float
    rates: dict[str, ExchangeRate] = field(default_factory=dict)

    def age(self, now: float) -> float:
        return max(0.0, now - self.fetched_at)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "league": self.league,
            "fetched_at": self.fetched_at,
            "rates": {key: rate.to_json() for key, rate in self.rates.items()},
        }

    @classmethod
    def from_json(cls, data: Any) -> LeagueRates | None:
        if not isinstance(data, Mapping) or data.get("version") != CACHE_VERSION:
            return None
        league, when = data.get("league"), data.get("fetched_at")
        if not isinstance(league, str) or not isinstance(when, (int, float)):
            return None
        rates: dict[str, ExchangeRate] = {}
        for key, value in (data.get("rates") or {}).items():
            rate = ExchangeRate.from_json(value)
            if rate is not None:
                rates[str(key)] = rate
        return cls(league=league, fetched_at=float(when), rates=rates)


class StaticIndex:
    """``item name -> bulk-exchange trade id``, from ``/api/trade/data/static``.

    The bulk exchange speaks in ids (``merricks-ducat``), and an item off the account
    endpoint carries only a name (``Merrick's Ducat``). poe.ninja's exchange lines
    carry both — but by definition we are here because poe.ninja has no line for this
    item, so its id has to come from GGG's own document. 195 kB, ``max-age=1800``,
    no rate-limit headers, cached for a day.
    """

    def __init__(self, entries: Mapping[str, str], fetched_at: float) -> None:
        self._entries = dict(entries)
        self.fetched_at = fetched_at

    def __len__(self) -> int:
        return len(self._entries)

    def age(self, now: float) -> float:
        return max(0.0, now - self.fetched_at)

    def trade_id(self, name: str) -> str | None:
        return self._entries.get(name.strip().casefold())

    def to_json(self) -> dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "fetched_at": self.fetched_at,
            "entries": self._entries,
        }

    @classmethod
    def from_json(cls, data: Any) -> StaticIndex | None:
        if not isinstance(data, Mapping) or data.get("version") != CACHE_VERSION:
            return None
        entries, when = data.get("entries"), data.get("fetched_at")
        if not isinstance(entries, Mapping) or not isinstance(when, (int, float)):
            return None
        return cls({str(k): str(v) for k, v in entries.items()}, float(when))

    @classmethod
    def from_payload(cls, payload: Any, fetched_at: float) -> StaticIndex:
        groups = payload.get("result") if isinstance(payload, Mapping) else None
        if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
            raise PricesError("the trade static document had no result array")
        entries: dict[str, str] = {}
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            for entry in group.get("entries") or ():
                if not isinstance(entry, Mapping):
                    continue
                text, trade_id = entry.get("text"), entry.get("id")
                if isinstance(text, str) and isinstance(trade_id, str) and text:
                    entries.setdefault(text.strip().casefold(), trade_id)
        if not entries:
            raise PricesError("the trade static document was empty")
        return cls(entries, fetched_at)


def parse_exchange_offers(payload: Any) -> tuple[dict[str, list[float]], int, int]:
    """One exchange response → ``({trade id: chaos rates}, returned, total)``.

    A row is counted only when the seller is online and is asking for **chaos**: the
    unit conversion for anything else would need a rate we are, by construction,
    already missing. ``exchange.amount / item.amount`` is the chaos price of one
    unit — the wire says "N chaos for M ducats", never a unit price.
    """
    if not isinstance(payload, Mapping):
        raise PricesError("the bulk exchange returned no object")
    results = payload.get("result")
    total = int(payload.get("total") or 0)
    rates: dict[str, list[float]] = {}
    if isinstance(results, Mapping):
        rows: Iterable[Any] = results.values()
        returned = len(results)
    elif isinstance(results, Sequence) and not isinstance(results, (str, bytes)):
        rows = results
        returned = len(results)
    else:
        return {}, 0, total
    for row in rows:
        listing = row.get("listing") if isinstance(row, Mapping) else None
        if not isinstance(listing, Mapping) or not _is_online(listing):
            continue
        for offer in listing.get("offers") or ():
            rate = _offer_rate(offer)
            if rate is None:
                continue
            trade_id, chaos = rate
            rates.setdefault(trade_id, []).append(chaos)
    for values in rates.values():
        values.sort()
    return rates, returned, total


def _offer_rate(offer: Any) -> tuple[str, float] | None:
    if not isinstance(offer, Mapping):
        return None
    paying, getting = offer.get("exchange"), offer.get("item")
    if not isinstance(paying, Mapping) or not isinstance(getting, Mapping):
        return None
    if str(paying.get("currency") or "").casefold() != "chaos":
        return None
    trade_id = getting.get("currency")
    have, want = paying.get("amount"), getting.get("amount")
    if not isinstance(trade_id, str) or not trade_id:
        return None
    if not _is_number(have) or not _is_number(want) or float(want) <= 0:
        return None
    chaos = float(have) / float(want)
    return (trade_id, chaos) if chaos > 0 else None


def _is_online(listing: Mapping[str, Any]) -> bool:
    account = listing.get("account")
    return isinstance(account, Mapping) and account.get("online") is not None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def median_of_cheapest(values: Sequence[float], sample: int) -> float | None:
    """The median of the cheapest ``sample``. See the module docstring."""
    usable = sorted(v for v in values if v > 0)[: max(1, sample)]
    if not usable:
        return None
    return float(statistics.median(usable))


class ExchangeClient:
    """Batched bulk-exchange lookups, cached per league on disk."""

    def __init__(
        self,
        net: NetApi,
        storage: Storage,
        *,
        clock: Callable[[], float] | None = None,
        rates_ttl: float = RATES_TTL,
        static_ttl: float = STATIC_TTL,
    ) -> None:
        self._net = net
        self._storage = storage
        self._clock = clock or time.time
        self._rates_ttl = rates_ttl
        self._static_ttl = static_ttl
        self._static: StaticIndex | None = None
        self._cached: dict[str, LeagueRates] = {}
        self.requests = 0
        """Every exchange-side request this client has made — including the static
        document. Read by tests and by the CLI, because "how many requests did that
        cost" is a question a user of a rate-limited API is entitled to ask."""

    def now(self) -> float:
        return self._clock()

    # -- the name → id document ------------------------------------------------

    async def static(self) -> StaticIndex:
        if self._static is None:
            self._static = StaticIndex.from_json(self._read(STATIC_CACHE_KEY))
        if self._static is not None and self._static.age(self.now()) < self._static_ttl:
            return self._static
        try:
            payload = await self._request("GET", STATIC_PATH, route=STATIC_ROUTE)
        except NetError as exc:
            if self._static is not None:
                _log.info("keeping the cached trade static document: %s", exc)
                return self._static
            raise PricesError(f"could not load bulk-exchange ids: {exc}") from None
        index = StaticIndex.from_payload(payload, self.now())
        self._static = index
        self._storage.write_json(STATIC_CACHE_KEY, index.to_json())
        return index

    # -- rates -----------------------------------------------------------------

    def cached(self, league: str) -> LeagueRates:
        """What we already know for ``league``, without touching the network."""
        held = self._cached.get(league)
        if held is None:
            held = LeagueRates.from_json(self._read(self._rates_key(league)))
            if held is None or held.league != league:
                held = LeagueRates(league=league, fetched_at=0.0)
            self._cached[league] = held
        if held.age(self.now()) >= self._rates_ttl:
            # Expired wholesale rather than per entry: one timestamp per league keeps
            # the record honest about when it was true, and these are cheap to redo.
            held = LeagueRates(league=league, fetched_at=self.now())
            self._cached[league] = held
        return held

    async def rates(
        self,
        names: Sequence[str],
        league: str,
        *,
        sample: int = EXCHANGE_MAX_WANTS,
        max_requeries: int = 4,
        batch: int = EXCHANGE_MAX_WANTS,
    ) -> dict[str, ExchangeRate]:
        """Bulk rates for ``names``, by item name. Cached names cost no request.

        Returns only the names that resolved to a trade id; a name GGG does not
        trade in bulk is simply absent, which the caller reports as ``unpriceable``.
        """
        if not names:
            return {}
        index = await self.static()
        held = self.cached(league)
        wanted: dict[str, str] = {}
        out: dict[str, ExchangeRate] = {}
        for name in names:
            trade_id = index.trade_id(name)
            if trade_id is None:
                continue
            known = held.rates.get(trade_id)
            if known is not None:
                out[name] = known
            else:
                wanted[trade_id] = name
        if not wanted:
            return out

        fresh = await self._query(list(wanted), league, sample=sample,
                                  max_requeries=max_requeries, batch=batch)
        held.fetched_at = held.fetched_at or self.now()
        held.rates.update(fresh)
        self._cached[league] = held
        self._storage.write_json(self._rates_key(league), held.to_json())
        for trade_id, name in wanted.items():
            rate = fresh.get(trade_id)
            if rate is not None:
                out[name] = rate
        return out

    async def _query(
        self,
        trade_ids: list[str],
        league: str,
        *,
        sample: int,
        max_requeries: int,
        batch: int,
    ) -> dict[str, ExchangeRate]:
        width = max(1, min(int(batch), EXCHANGE_MAX_WANTS))
        collected: dict[str, tuple[list[float], int, bool]] = {}
        for start in range(0, len(trade_ids), width):
            group = trade_ids[start : start + width]
            rates, returned, total = await self._post(group, league)
            capped = total > returned or returned >= RESULT_CAP
            for trade_id in group:
                collected[trade_id] = (rates.get(trade_id, []), total, capped)

        # Only the starved wants are re-queried: a want that came back with at least
        # `sample` offers already holds *its own* cheapest `sample`, because the
        # server sorted the whole batch ascending. See the module docstring.
        starved = [
            trade_id
            for trade_id, (values, _total, capped) in collected.items()
            if capped and len(values) < sample and width > 1
        ]
        for trade_id in starved[: max(0, max_requeries)]:
            rates, returned, total = await self._post([trade_id], league)
            collected[trade_id] = (rates.get(trade_id, []), total, total > returned)

        out: dict[str, ExchangeRate] = {}
        for trade_id, (values, total, capped) in collected.items():
            out[trade_id] = ExchangeRate(
                trade_id=trade_id,
                chaos=median_of_cheapest(values, sample),
                offers=len(values),
                total=total,
                truncated=bool(capped and len(values) < sample),
            )
        return out

    async def _post(
        self, trade_ids: Sequence[str], league: str
    ) -> tuple[dict[str, list[float]], int, int]:
        body = {
            "query": {
                "status": {"option": "online"},
                "have": ["chaos"],
                "want": list(trade_ids),
            },
            # Ascending, which is what makes the cheapest-N sample meaningful and
            # also what makes a truncated batch a *floor* rather than noise.
            "sort": {"have": "asc"},
            "engine": "new",
        }
        payload = await self._request(
            "POST", f"{EXCHANGE_PATH}/{urlquote(league)}", json=body, route=EXCHANGE_ROUTE
        )
        return parse_exchange_offers(payload)

    # -- internals -------------------------------------------------------------

    def _rates_key(self, league: str) -> str:
        import hashlib

        digest = hashlib.sha1(f"{RATES_CACHE_PREFIX}:{league}".encode()).hexdigest()
        return f"{RATES_CACHE_PREFIX}-{digest}.json"

    def _read(self, key: str) -> Any:
        try:
            return self._storage.read_json(key)
        except Exception as exc:  # a corrupt cache must never be fatal
            _log.warning("discarding unreadable exchange cache: %s", exc)
            return None

    async def _request(
        self, method: str, path: str, *, json: Any = None, route: str
    ) -> Any:
        self.requests += 1
        if method == "POST":
            return await self._net.post_json(
                path, json=json, authenticated=False, route=route
            )
        return await self._net.get_json(path, authenticated=False, route=route)
