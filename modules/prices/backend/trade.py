"""Tier 3 — the official trade API (SPEC §5.3).

**Wired, not eager.** Nothing in :mod:`.valuation` can reach this file, and
:meth:`PricesModule.quote` is the only way in. That is the whole design constraint:
the 2020 POE Overlay ban wave was API abuse *plus auto-pricing*, and auto-pricing is
precisely what calling this on every zone transition would be.

Four rules, all of them load-bearing.

**Stat filters key off opaque ids.** ``explicit.stat_1509134228``, never the readable
text. ``/api/trade/data/stats`` is the only place those ids exist; it is a 400 kB
document with a ``max-age=1799``, so it is fetched once and cached on disk for a day.
A query built from mod text silently matches nothing.

**Median of the cheapest N, not the minimum.** The cheapest listing is very often a
bot, a typo, or someone who logged off in the last league. The median of the sample
is a number the player can actually realise.

**Online sellers only.** ``listing.account.online`` is present when the seller is
logged in. The search's own ``status: online`` filter is not enough on its own —
sellers go offline between the index and the fetch — so it is applied again here.

**Two buckets, neither of them the item bucket.** Search and fetch are governed by
``trade-search-request-limit`` and ``trade-fetch-request-limit``, learned from the
response headers like everything else. `net` keys buckets by policy, so as long as
these routes are distinct from the item route they cannot starve a sync.

The requests are anonymous. Trade search needs no credential, and sending the
account's session cookie to it would tie a public query to the account for no gain.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote as urlquote

from modules.net.backend.api import NetApi, NetError
from modules.poeapi.backend.api import NormalizedItem, Rarity
from modules.prices.backend.api import TradeQuote, TradeUnavailable
from runtime.log import get_logger
from runtime.storage import Storage

__all__ = [
    "FETCH_ROUTE",
    "MAX_FETCH_IDS",
    "SEARCH_ROUTE",
    "STATS_PATH",
    "STATS_TTL",
    "StatIndex",
    "TradeClient",
    "build_query",
    "median_of_cheapest",
]

_log = get_logger("module.prices.trade")

SEARCH_PATH = "/api/trade/search"
FETCH_PATH = "/api/trade/fetch"
STATS_PATH = "/api/trade/data/stats"
SITE_SEARCH_URL = "https://www.pathofexile.com/trade/search"

SEARCH_ROUTE = "trade:search"
FETCH_ROUTE = "trade:fetch"
STATS_ROUTE = "trade:stats"

MAX_FETCH_IDS = 10
"""The fetch endpoint's own limit. Asking for eleven is a 400."""

STATS_TTL = 86400.0
"""One day. The document is ``max-age=1799`` but its contents change with a patch,
not with the hour, and it is 400 kB."""

STATS_CACHE_KEY = "trade-stats.json"
STATS_CACHE_VERSION = 1


class StatIndex:
    """``mod text -> opaque stat id``, cached on disk.

    The text is normalized by replacing every number with ``#``, which is the form
    GGG's own document uses: an item's ``+58 to maximum Life`` has to be matched
    against the entry ``+# to maximum Life``.
    """

    def __init__(self, entries: Mapping[str, str], fetched_at: float) -> None:
        self._entries = dict(entries)
        self.fetched_at = fetched_at

    def __len__(self) -> int:
        return len(self._entries)

    def age(self, now: float) -> float:
        return max(0.0, now - self.fetched_at)

    def stat_id(self, text: str) -> str | None:
        return self._entries.get(normalize_stat_text(text))

    def to_json(self) -> dict[str, Any]:
        return {
            "version": STATS_CACHE_VERSION,
            "fetched_at": self.fetched_at,
            "entries": self._entries,
        }

    @classmethod
    def from_json(cls, data: Any) -> StatIndex | None:
        if not isinstance(data, Mapping) or data.get("version") != STATS_CACHE_VERSION:
            return None
        entries = data.get("entries")
        fetched_at = data.get("fetched_at")
        if not isinstance(entries, Mapping) or not isinstance(fetched_at, (int, float)):
            return None
        return cls(
            {str(k): str(v) for k, v in entries.items()}, float(fetched_at)
        )

    @classmethod
    def from_payload(cls, payload: Any, fetched_at: float) -> StatIndex:
        """Read ``/api/trade/data/stats``.

        Later groups do not overwrite earlier ones: ``pseudo`` comes first in GGG's
        document and its aggregate stats would otherwise shadow the explicit mods an
        item actually has.
        """
        entries: dict[str, str] = {}
        groups = payload.get("result") if isinstance(payload, Mapping) else None
        if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
            raise TradeUnavailable("the trade stats document had no result array")
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            group_entries = group.get("entries")
            if not isinstance(group_entries, Sequence) or isinstance(group_entries, (str, bytes)):
                continue
            for entry in group_entries:
                if not isinstance(entry, Mapping):
                    continue
                text, stat_id = entry.get("text"), entry.get("id")
                if isinstance(text, str) and isinstance(stat_id, str):
                    entries.setdefault(normalize_stat_text(text), stat_id)
        if not entries:
            raise TradeUnavailable("the trade stats document was empty")
        return cls(entries, fetched_at)


def normalize_stat_text(text: str) -> str:
    """``+58 to maximum Life`` → ``+# to maximum Life``.

    Ranges (``Adds 12 to 30 Physical Damage``) collapse to ``Adds # to #``, which is
    exactly how GGG spells them in the stats document.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isdigit():
            while index < length and (text[index].isdigit() or text[index] == "."):
                index += 1
            out.append("#")
            continue
        out.append(char)
        index += 1
    return "".join(out).strip()


def build_query(item: NormalizedItem, stats: StatIndex | None) -> dict[str, Any]:
    """The POST body for one item.

    Uniques are searched by name and base type, which is precise and needs no stat
    filters at all. Rares get their explicit mods turned into id filters, and any mod
    whose id is unknown is **dropped rather than guessed** — a query with three of
    five filters returns a superset, which is a wide answer; a query with a wrong
    filter returns nothing, which reads as "worthless".
    """
    query: dict[str, Any] = {
        "status": {"option": "online"},
        "stats": [{"type": "and", "filters": []}],
    }
    if item.rarity is Rarity.UNIQUE and item.name:
        query["name"] = item.name
        if item.base_type:
            query["type"] = item.base_type
    elif item.base_type:
        query["type"] = item.base_type

    filters: list[dict[str, Any]] = []
    if stats is not None and item.rarity is not Rarity.UNIQUE:
        for text in (*item.mods.explicit, *item.mods.implicit):
            stat_id = stats.stat_id(text)
            if stat_id:
                filters.append({"id": stat_id, "disabled": False})
    query["stats"] = [{"type": "and", "filters": filters}]
    if item.corrupted:
        query.setdefault("filters", {}).setdefault("misc_filters", {}).setdefault(
            "filters", {}
        )["corrupted"] = {"option": "true"}
    return {"query": query, "sort": {"price": "asc"}}


def median_of_cheapest(values: Sequence[float]) -> float | None:
    """The median of an already-cheapest-first sample. See the module docstring."""
    usable = sorted(v for v in values if v > 0)
    if not usable:
        return None
    return float(statistics.median(usable))


class TradeClient:
    """Search, fetch, and the price conversion in between."""

    def __init__(
        self,
        net: NetApi,
        storage: Storage,
        *,
        clock: Callable[[], float] | None = None,
        stats_ttl: float = STATS_TTL,
    ) -> None:
        self._net = net
        self._storage = storage
        self._clock = clock or time.time
        self._stats_ttl = stats_ttl
        self._stats: StatIndex | None = None
        self.requests = 0
        """Every trade request this client has made. Read by tests and diagnostics —
        the guarantee that a valuation pass makes none is only worth as much as the
        counter that proves it."""

    def now(self) -> float:
        return self._clock()

    # -- stat ids --------------------------------------------------------------

    async def stats(self, *, refresh: bool = False) -> StatIndex:
        if self._stats is None:
            self._stats = StatIndex.from_json(self._read_cached_stats())
        fresh = self._stats is not None and self._stats.age(self.now()) < self._stats_ttl
        if fresh and not refresh:
            assert self._stats is not None
            return self._stats
        try:
            payload = await self._request("GET", STATS_PATH, route=STATS_ROUTE)
        except NetError as exc:
            if self._stats is not None:
                _log.info("keeping cached trade stats: %s", exc)
                return self._stats
            raise TradeUnavailable(f"could not load trade stat ids: {exc}") from None
        index = StatIndex.from_payload(payload, self.now())
        self._stats = index
        self._storage.write_json(STATS_CACHE_KEY, index.to_json())
        return index

    def _read_cached_stats(self) -> Any:
        try:
            return self._storage.read_json(STATS_CACHE_KEY)
        except Exception as exc:  # a corrupt cache must never be fatal
            _log.warning("discarding unreadable trade stat cache: %s", exc)
            return None

    # -- the quote -------------------------------------------------------------

    async def quote(
        self,
        item: NormalizedItem,
        league: str,
        *,
        chaos_of: Callable[[str], float | None],
        sample: int = MAX_FETCH_IDS,
    ) -> TradeQuote:
        """One on-demand price. Two requests: one search, one fetch."""
        stats: StatIndex | None = None
        if item.rarity is not Rarity.UNIQUE and item.mods.total:
            # Deliberately not caught: a rare searched without stat filters is a
            # base-type search, which is too wide to be a price. `TradeUnavailable`
            # is a better answer than a misleading number.
            stats = await self.stats()
        body = build_query(item, stats)
        search = await self._request(
            "POST", f"{SEARCH_PATH}/{urlquote(league)}", json=body, route=SEARCH_ROUTE
        )
        if not isinstance(search, Mapping):
            raise TradeUnavailable("the trade search returned no result")
        query_id = search.get("id")
        ids = search.get("result")
        total = int(search.get("total") or 0)
        if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes)) or not ids:
            return TradeQuote(None, considered=0, online=0, total=total)

        wanted = [str(entry) for entry in ids[: max(1, min(sample, MAX_FETCH_IDS))]]
        fetched = await self._request(
            "GET",
            f"{FETCH_PATH}/{','.join(wanted)}",
            params={"query": query_id} if isinstance(query_id, str) else None,
            route=FETCH_ROUTE,
        )
        results = fetched.get("result") if isinstance(fetched, Mapping) else None
        if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
            return TradeQuote(None, considered=0, online=0, total=total)

        prices: list[float] = []
        online = 0
        for entry in results:
            if not isinstance(entry, Mapping):
                continue
            listing = entry.get("listing")
            if not isinstance(listing, Mapping) or not _is_online(listing):
                continue
            online += 1
            chaos = _listing_chaos(listing, chaos_of)
            if chaos is not None:
                prices.append(chaos)
        return TradeQuote(
            median_of_cheapest(prices),
            considered=len(results),
            online=online,
            total=total,
            listings=sorted(prices),
            query_url=(
                f"{SITE_SEARCH_URL}/{urlquote(league)}/{query_id}"
                if isinstance(query_id, str)
                else None
            ),
        )

    # -- internals -------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        route: str,
    ) -> Any:
        self.requests += 1
        if method == "POST":
            return await self._net.post_json(
                path, json=json, params=params, authenticated=False, route=route
            )
        return await self._net.get_json(path, params=params, authenticated=False, route=route)


def _is_online(listing: Mapping[str, Any]) -> bool:
    account = listing.get("account")
    return isinstance(account, Mapping) and bool(account.get("online"))


def _listing_chaos(
    listing: Mapping[str, Any], chaos_of: Callable[[str], float | None]
) -> float | None:
    price = listing.get("price")
    if not isinstance(price, Mapping):
        return None
    amount = price.get("amount")
    currency = price.get("currency")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    if not isinstance(currency, str):
        return None
    rate = chaos_of(currency)
    if rate is None:
        return None
    return float(amount) * rate
