"""Tier 3 — the official trade API (SPEC §5.3).

**Wired, not eager.** Nothing in :mod:`.valuation` can reach this file, and
:meth:`PricesModule.quote` is the only way in. That is the whole design constraint:
the 2020 POE Overlay ban wave was API abuse *plus auto-pricing*, and auto-pricing is
precisely what calling this on every zone transition would be.

Five rules, all of them load-bearing.

**A query is a sample, not a fingerprint.** The first live appraisal ANDed every
resolvable mod on an item into one filter set, on the theory that "a query with three
of five filters returns a superset". The theory was right and the code did not
implement it: a six-mod rare became a seven-filter conjunction, two of three flagged
items matched **zero** listings, and the third matched exactly **one** — whose asking
price was then reported as a "median". So the query is now built from a *subset*
chosen by the caller (:class:`~modules.prices.backend.api.ModFocus`), rolls are
matched as widened ranges rather than exact values, and a search that still finds
nothing gets one automatic broadening retry. See :func:`build_plan`.

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

import math
import re
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote as urlquote

from modules.net.backend.api import NetApi, NetError
from modules.poeapi.backend.api import NormalizedItem, Rarity
from modules.prices.backend.api import ModFocus, TradeQuote, TradeUnavailable
from runtime.log import get_logger
from runtime.storage import Storage

__all__ = [
    "FETCH_ROUTE",
    "MAX_FETCH_IDS",
    "MAX_STAT_FILTERS",
    "SEARCH_ROUTE",
    "STATS_PATH",
    "STATS_TTL",
    "WIDEN",
    "QueryStep",
    "StatIndex",
    "TradeClient",
    "build_plan",
    "build_query",
    "median_of_cheapest",
    "significant_mods",
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

MAX_STAT_FILTERS = 2
"""How many mods one query may filter on.

Two, from measurement rather than taste. Against the live Allflame economy:
``Amethyst Ring`` + ``max life ≥ 82`` returned 2524 listings; ``Dragonscale
Gauntlets`` + ``max life ≥ 87`` returned 198; ``Searching Eye Jewel`` + ``+# to
Strength and Dexterity`` returned 438. The same three items with *every* mod ANDed
returned 0, 0 and 1. The gate rarely produces more than two mod-derived reasons
anyway, so this cap mostly documents the ceiling rather than enforcing it."""

WIDEN = 0.2
"""How far below the observed roll the ``min`` filter sits — 20%.

An exact-value filter on a random roll matches almost nothing, and the trade site's
own filters are ranges for exactly this reason. 20% is wide enough that a ``+103 to
maximum Life`` ring finds the ``+85`` to ``+99`` tier band it actually competes with,
and narrow enough that it does not drift into a different mod tier entirely."""

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


_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# Rarities the trade site can filter on directly. A rare is priced against rares:
# leaving the rarity open lets a unique of the same base — which is priced by its
# unique name, not its mods — into a sample it has no business being in.
_RARITY_OPTION: Mapping[Rarity, str] = {
    Rarity.RARE: "rare",
    Rarity.MAGIC: "magic",
    Rarity.NORMAL: "normal",
}


@dataclass(frozen=True, slots=True)
class QueryStep:
    """One search this item is willing to make, and what it asked for in words."""

    body: dict[str, Any]
    description: str
    filters: tuple[str, ...] = field(default=())

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"QueryStep({self.description!r})"


def first_number(text: str) -> float | None:
    """The first number in a mod line — ``+103 to maximum Life`` → ``103.0``."""
    match = _NUMBER.search(text)
    return float(match.group()) if match else None


def significant_mods(item: NormalizedItem, limit: int = MAX_STAT_FILTERS) -> list[ModFocus]:
    """A fallback focus for a caller that did not supply one.

    Deliberately dumb — it prefers the mods with the largest leading number, which is
    a proxy for "the roll somebody would search on" and nothing more. It exists so
    ``prices.quote`` remains callable on its own (``poedex price <uid>``) without
    reproducing the tier-2 gate inside `prices`, which would be a dependency cycle
    wearing a disguise. When a caller *does* know why the item is interesting — and
    `appraisal` always does, because its gate just decided — its focus wins.
    """
    scored: list[tuple[float, ModFocus]] = []
    for text in (*item.mods.explicit, *item.mods.fractured, *item.mods.implicit):
        value = first_number(text)
        scored.append((value or 0.0, ModFocus(text=text, minimum=None, label=text)))
    scored.sort(key=lambda pair: -pair[0])
    return [focus for _value, focus in scored[:limit]]


def widened(value: float, *, widen: float = WIDEN) -> float:
    """A roll, dropped ``widen`` below itself and rounded down to something a human
    would have typed. ``103 → 82``, ``0.34 → 0.27``."""
    floor = value * (1.0 - widen)
    return float(math.floor(floor)) if floor >= 1 else round(floor, 2)


def _stat_filter(focus: ModFocus, stat_id: str) -> tuple[dict[str, Any], str]:
    entry: dict[str, Any] = {"id": stat_id, "disabled": False}
    label = focus.label or focus.text
    if focus.minimum is not None:
        entry["value"] = {"min": focus.minimum}
        return entry, f"{label} ≥ {focus.minimum:g}"
    return entry, f"{label} (any roll)"


def build_plan(
    item: NormalizedItem,
    stats: StatIndex | None,
    focus: Sequence[ModFocus] | None = None,
    *,
    limit: int = MAX_STAT_FILTERS,
) -> tuple[QueryStep, ...]:
    """The ladder of searches for one item, widest last.

    Step 1 is the honest question: *this base type, in this rarity, with the mods that
    made it interesting, rolled about this well.* Step 2 exists only for when step 1
    matches nothing — it keeps the single most significant filter and drops the roll
    floor entirely, which is the smallest broadening that can change a zero.

    There is no step 3. A bare base-type search is not a price for a rare; it is the
    price of the cheapest junk sharing its base, and reporting that as this item's
    value is worse than reporting nothing. The one exception is an item with no
    resolvable mods at all, where base type genuinely is everything the tool knows —
    that query is made, and :attr:`QueryStep.description` says so, so a surprising
    number can be traced to it.

    Uniques skip the ladder: name plus base type is exact, and no stat filter can
    improve on it.
    """
    if item.rarity is Rarity.UNIQUE and item.name:
        body = _query_body(item, [])
        body["query"]["name"] = item.name
        return (QueryStep(body, f"{item.name} · {item.base_type}".strip(" ·")),)

    chosen = list(focus) if focus is not None else significant_mods(item, limit)
    resolved: list[tuple[ModFocus, str]] = []
    if stats is not None:
        for entry in chosen:
            stat_id = stats.stat_id(entry.text)
            if stat_id is None:
                # Dropped rather than guessed: a wrong id returns nothing, which reads
                # as "worthless", and that is the one wrong answer this tier must not
                # give.
                _log.debug("no trade stat id for %r; dropping it from the query", entry.text)
                continue
            resolved.append((entry, stat_id))
            if len(resolved) >= limit:
                break

    base = _describe_base(item)
    if not resolved:
        body = _query_body(item, [])
        return (QueryStep(body, f"{base} · base type only — no mod resolved to a filter"),)

    entries = [_stat_filter(entry, stat_id) for entry, stat_id in resolved]
    step1 = QueryStep(
        _query_body(item, [pair[0] for pair in entries]),
        f"{base} · " + " · ".join(pair[1] for pair in entries),
        tuple(pair[1] for pair in entries),
    )

    # The broadening: one filter, no floor. Same shape as step 1 so a reader can see
    # what was given up rather than diffing two hand-built dicts.
    widest, widest_stat_id = resolved[0]
    loose = ModFocus(text=widest.text, minimum=None, label=widest.label)
    entry, words = _stat_filter(loose, widest_stat_id)
    step2 = QueryStep(
        _query_body(item, [entry]),
        f"{base} · {words} — broadened after no matches",
        (words,),
    )
    if step2.body == step1.body:
        return (step1,)
    return (step1, step2)


def _describe_base(item: NormalizedItem) -> str:
    rarity = _RARITY_OPTION.get(item.rarity)
    return " · ".join(part for part in (item.base_type, rarity) if part)


def _query_body(item: NormalizedItem, filters: Sequence[dict[str, Any]]) -> dict[str, Any]:
    query: dict[str, Any] = {
        "status": {"option": "online"},
        "stats": [{"type": "and", "filters": list(filters)}],
    }
    if item.base_type:
        query["type"] = item.base_type
    rarity = _RARITY_OPTION.get(item.rarity)
    if rarity:
        query.setdefault("filters", {}).setdefault("type_filters", {}).setdefault(
            "filters", {}
        )["rarity"] = {"option": rarity}
    if item.corrupted:
        query.setdefault("filters", {}).setdefault("misc_filters", {}).setdefault(
            "filters", {}
        )["corrupted"] = {"option": "true"}
    return {"query": query, "sort": {"price": "asc"}}


def build_query(
    item: NormalizedItem,
    stats: StatIndex | None,
    focus: Sequence[ModFocus] | None = None,
) -> dict[str, Any]:
    """The POST body for one item — the first step of :func:`build_plan`."""
    return build_plan(item, stats, focus)[0].body


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
        focus: Sequence[ModFocus] | None = None,
        retry_on_empty: bool = True,
    ) -> TradeQuote:
        """One on-demand price. Two requests normally; three if the first search
        matched nothing and the broadening retry ran.

        A zero-result search is returned as a quote with ``chaos is None`` and
        ``total == 0`` — an *answer*, not an absence. The caller needs it that way to
        say "no matching listings" instead of "pricing…" forever.
        """
        stats: StatIndex | None = None
        if item.rarity is not Rarity.UNIQUE and item.mods.total:
            # Deliberately not caught: a rare searched without stat filters is a
            # base-type search, which is too wide to be a price. `TradeUnavailable`
            # is a better answer than a misleading number.
            stats = await self.stats()
        plan = build_plan(item, stats, focus)
        steps = plan if retry_on_empty else plan[:1]

        attempts = 0
        query_id: Any = None
        ids: Any = None
        total = 0
        step = steps[0]
        for step in steps:
            attempts += 1
            _log.debug("trade search %d/%d: %s", attempts, len(steps), step.description)
            search = await self._request(
                "POST",
                f"{SEARCH_PATH}/{urlquote(league)}",
                json=step.body,
                route=SEARCH_ROUTE,
            )
            if not isinstance(search, Mapping):
                raise TradeUnavailable("the trade search returned no result")
            query_id = search.get("id")
            ids = search.get("result")
            total = int(search.get("total") or 0)
            if isinstance(ids, Sequence) and not isinstance(ids, (str, bytes)) and ids:
                break

        def _empty(reason_total: int) -> TradeQuote:
            return TradeQuote(
                None,
                considered=0,
                online=0,
                total=reason_total,
                query=step.description,
                attempts=attempts,
                query_url=(
                    f"{SITE_SEARCH_URL}/{urlquote(league)}/{query_id}"
                    if isinstance(query_id, str)
                    else None
                ),
            )

        if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes)) or not ids:
            return _empty(total)

        wanted = [str(entry) for entry in ids[: max(1, min(sample, MAX_FETCH_IDS))]]
        fetched = await self._request(
            "GET",
            f"{FETCH_PATH}/{','.join(wanted)}",
            params={"query": query_id} if isinstance(query_id, str) else None,
            route=FETCH_ROUTE,
        )
        results = fetched.get("result") if isinstance(fetched, Mapping) else None
        if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
            return _empty(total)

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
            query=step.description,
            attempts=attempts,
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
