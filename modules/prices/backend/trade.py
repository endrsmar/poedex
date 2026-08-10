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
from modules.prices.backend.api import (
    ModFocus,
    QuerySpec,
    TradeQuote,
    TradeUnavailable,
)
from runtime.log import get_logger
from runtime.storage import Storage

__all__ = [
    "FETCH_ROUTE",
    "MAX_FETCH_IDS",
    "MAX_STAT_FILTERS",
    "OPEN_PREFIX_TEXT",
    "OPEN_SUFFIX_TEXT",
    "SEARCH_ROUTE",
    "STATS_PATH",
    "STATS_TTL",
    "STAT_GROUP_PRIORITY",
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
STATS_CACHE_VERSION = 3
"""Bumped in Phase 9, and again in Phase 9b. A version-1 cache is a flat
``text -> id`` map built by the first-group-wins rule that put ``pseudo`` ids on
explicit mods; a version-2 cache threw the ``(Local)`` entries away, so every local
defence and weapon mod either resolved to the global id or to nothing. Both are
discarded rather than migrated, because the ids in them are the bug."""

LOCAL_SUFFIXES = (" (Local)", " (Shields)", " (Staves)")
"""GGG's markers for the on-the-item reading of a sentence it publishes twice.

``#% increased Armour`` is what a ring grants; ``#% increased Armour (Local)`` is what
a body armour rolls, and they are different ids. Before Phase 9b these suffixes were
left on the key, so the local entries were filed under sentences no item ever writes
and the global id won by default. Measured live: on rare body armours the global id
matched **0** listings and the local id matched 10 000+.

The other two suffixes GGG uses are deliberately absent. ``(Maps)`` scopes a stat to
the map device rather than to the item, and ``(Legacy)`` marks an id kept alive for
items that can no longer drop; either one folded in here would answer a filter
question with a sentence that does not mean the same thing."""


def strip_local(text: str) -> tuple[str, bool]:
    """``("#% increased Armour", True)`` for ``"#% increased Armour (Local)"``."""
    for suffix in LOCAL_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)], True
    return text, False

STAT_GROUP_PRIORITY: tuple[str, ...] = (
    "explicit",
    "fractured",
    "implicit",
    "crafted",
    "enchant",
    "veiled",
    "scourge",
    "sanctum",
    "crucible",
    "delve",
    "monster",
    "ultimatum",
    "skill",
)
"""Which group answers when a sentence exists in several — **pseudo is never it**.

``Adds # to # Physical Damage`` appears under ``pseudo`` *and* ``explicit``, and
``pseudo`` comes first in GGG's document. The old index used
``entries.setdefault()``, so the first group won and every such search filtered on
``pseudo.pseudo_adds_physical_damage`` — an aggregate over every source of physical
damage on the item, including the implicit and the flat added damage on the base.
That is a different question from "this item has this mod", and it is wider in a way
that is invisible from the query description.

Groups not named here rank after every named one and before ``pseudo``, so a group
GGG adds next league is preferred over an aggregate without anyone editing this
tuple."""

_PSEUDO_GROUP = "pseudo"

OPEN_PREFIX_TEXT = "# Empty Prefix Modifiers"
OPEN_SUFFIX_TEXT = "# Empty Suffix Modifiers"
"""How GGG spells the open-affix counts in ``/api/trade/data/stats``.

They are ``pseudo`` entries and the *only* pseudo ids this file will ever use on
purpose, which is why they are resolved by text through the index like every other
filter rather than hardcoded as ``pseudo.pseudo_number_of_empty_prefix_mods``. If a
league renames them the filter disappears and the query description says so; it does
not silently filter on an id that no longer means what it did."""


def _group_rank(group: str) -> int:
    if group == _PSEUDO_GROUP:
        return len(STAT_GROUP_PRIORITY) + 1
    try:
        return STAT_GROUP_PRIORITY.index(group)
    except ValueError:
        return len(STAT_GROUP_PRIORITY)


class StatIndex:
    """``mod text -> {group -> opaque stat id}``, cached on disk.

    The text is normalized by replacing every number with ``#``, which is the form
    GGG's own document uses: an item's ``+58 to maximum Life`` has to be matched
    against the entry ``+# to maximum Life``.

    **The group is kept, not collapsed.** One sentence resolves to as many ids as
    there are groups that can produce it — ``explicit.stat_3299347043``,
    ``crafted.stat_3299347043``, ``pseudo.pseudo_total_life`` — and which one a query
    should use depends on where the line came from on the item. Flattening that to
    one id per sentence is what let ``pseudo`` shadow ``explicit``; see
    :data:`STAT_GROUP_PRIORITY`.
    """

    def __init__(
        self,
        entries: Mapping[str, Mapping[str, str]],
        fetched_at: float,
        local: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self._entries: dict[str, dict[str, str]] = {
            str(text): {str(group): str(sid) for group, sid in ids.items()}
            for text, ids in entries.items()
        }
        # The same shape for the sentences GGG publishes twice, keyed by the sentence
        # *without* its ``(Local)`` suffix — which is how an item writes it.
        self._local: dict[str, dict[str, str]] = {
            str(text): {str(group): str(sid) for group, sid in ids.items()}
            for text, ids in (local or {}).items()
        }
        self.fetched_at = fetched_at

    def __len__(self) -> int:
        return len(self._entries)

    def age(self, now: float) -> float:
        return max(0.0, now - self.fetched_at)

    def _keys(self, text: str) -> tuple[str, ...]:
        """The document keys a line could be filed under, in preference order.

        GGG publishes one sentence per *stat*, spelled with the sign the format string
        carries — ``+# to Total Mana Cost of Skills``, ``+# Physical Damage taken from
        Attack Hits`` — and an item that rolled the beneficial direction writes the
        same sentence with a minus. Both were checked against the live document:
        ``explicit.stat_3736589033`` and ``explicit.stat_3441651621`` exist only under
        ``+#``. About ten gear sentences are in this family, and every one of them was
        being dropped from queries as "no trade filter for this" when the filter is
        right there under the other sign.

        Tried second, never first. ``-#`` and ``+#`` really are different stats for a
        handful of sentences GGG publishes both ways, and an exact hit is always the
        answer.
        """
        key = normalize_stat_text(text)
        signed = key.replace("-#", "+#")
        return (key,) if signed == key else (key, signed)

    def stat_ids(self, text: str, *, local: bool = False) -> Mapping[str, str]:
        """Every id this sentence has, keyed by group. Empty when it has none.

        Exposed so the pseudo-shadowing fix is falsifiable from outside: a test can
        assert that ``Adds # to # Physical Damage`` has *both* an explicit and a
        pseudo id and that :meth:`stat_id` returns the explicit one — and, with
        ``local=True``, that it has a third id again that neither of those is.
        """
        table = self._local if local else self._entries
        for key in self._keys(text):
            if key in table:
                return dict(table[key])
        return {}

    def stat_id(
        self, text: str, *, origin: str = "explicit", local: bool | None = None
    ) -> str | None:
        """The id a filter for ``text`` should use.

        ``origin`` is where the line sits on the item — ``explicit``, ``crafted``,
        ``fractured``, ``implicit``, ``enchant``. When the document has an id in that
        exact group it wins, because a crafted ``+# to maximum Life`` searched as an
        explicit one excludes every item whose life *is* the bench craft. Otherwise
        :data:`STAT_GROUP_PRIORITY` decides, and ``pseudo`` is last in every case.

        ``local`` is the other axis, and it is the one this document cannot answer for
        itself. Twenty-two sentences exist in both readings and mean different stats;
        `moddb` knows which one a given mod is (:attr:`ModMatch.local`) and passes it
        down through :attr:`ModFocus.local`. ``None`` means nobody knew: the global
        reading wins, except where there is no global reading at all — six common
        sentences including ``#% increased Energy Shield`` exist only locally, and
        answering ``None`` for those is how they were dropped from queries before.
        """
        plain = localised = None
        for key in self._keys(text):
            plain = plain or self._entries.get(key)
            localised = localised or self._local.get(key)
        if local is True:
            ids = localised or plain
        elif local is False:
            ids = plain
        else:
            ids = plain or localised
        if not ids:
            return None
        if origin in ids:
            return ids[origin]
        best = min(ids, key=lambda group: (_group_rank(group), group))
        return ids[best]

    def to_json(self) -> dict[str, Any]:
        return {
            "version": STATS_CACHE_VERSION,
            "fetched_at": self.fetched_at,
            "entries": self._entries,
            "local": self._local,
        }

    @classmethod
    def from_json(cls, data: Any) -> StatIndex | None:
        if not isinstance(data, Mapping) or data.get("version") != STATS_CACHE_VERSION:
            return None
        entries = data.get("entries")
        fetched_at = data.get("fetched_at")
        if not isinstance(entries, Mapping) or not isinstance(fetched_at, (int, float)):
            return None
        rebuilt: dict[str, dict[str, str]] = {}
        for text, ids in entries.items():
            if not isinstance(ids, Mapping):
                return None
            rebuilt[str(text)] = {str(group): str(sid) for group, sid in ids.items()}
        local_raw = data.get("local")
        local: dict[str, dict[str, str]] = {}
        if isinstance(local_raw, Mapping):
            for text, ids in local_raw.items():
                if not isinstance(ids, Mapping):
                    return None
                local[str(text)] = {str(group): str(sid) for group, sid in ids.items()}
        return cls(rebuilt, float(fetched_at), local)

    @classmethod
    def from_payload(cls, payload: Any, fetched_at: float) -> StatIndex:
        """Read ``/api/trade/data/stats``, keeping every group a sentence appears in.

        Nothing is dropped here and nothing shadows anything: the choice between two
        ids for the same sentence is made at lookup time by :meth:`stat_id`, where
        the caller's ``origin`` is known.
        """
        entries: dict[str, dict[str, str]] = {}
        local: dict[str, dict[str, str]] = {}
        groups = payload.get("result") if isinstance(payload, Mapping) else None
        if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
            raise TradeUnavailable("the trade stats document had no result array")
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            group_entries = group.get("entries")
            if not isinstance(group_entries, Sequence) or isinstance(group_entries, (str, bytes)):
                continue
            group_id = group.get("id")
            for entry in group_entries:
                if not isinstance(entry, Mapping):
                    continue
                text, stat_id = entry.get("text"), entry.get("id")
                if not isinstance(text, str) or not isinstance(stat_id, str):
                    continue
                # The entry's own `type` is the authority; the group header's `id` is
                # the fallback, and the id prefix the last resort — a document that
                # labels neither still has `explicit.stat_960081730` to read.
                kind = entry.get("type")
                name = (
                    kind
                    if isinstance(kind, str) and kind
                    else group_id
                    if isinstance(group_id, str) and group_id
                    else stat_id.split(".", 1)[0]
                )
                # ``#% increased Armour (Local)`` is the same sentence an item writes
                # as ``#% increased Armour``; the suffix is GGG's way of saying which
                # stat it means, not part of the text to match against. Left on the
                # key it filed 22 entries under sentences nothing ever writes.
                key, is_local = strip_local(text)
                table = local if is_local else entries
                table.setdefault(normalize_stat_text(key), {}).setdefault(name, stat_id)
        if not entries:
            raise TradeUnavailable("the trade stats document was empty")
        return cls(entries, fetched_at, local)


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
    """One filter entry, plus the words that describe it.

    ``max`` is not a variant spelling of ``min``. It is what "at least this good" means
    for a mod whose ladder runs downwards, and there are about ten of those on gear —
    see :attr:`ModFocus.maximum`.
    """
    entry: dict[str, Any] = {"id": stat_id, "disabled": False}
    label = focus.label or focus.text
    if focus.minimum is not None:
        entry["value"] = {"min": focus.minimum}
        return entry, f"{label} ≥ {focus.minimum:g}"
    if focus.maximum is not None:
        entry["value"] = {"max": focus.maximum}
        return entry, f"{label} ≤ {focus.maximum:g}"
    return entry, f"{label} (any roll)"


def _open_affix_filters(
    spec: QuerySpec, stats: StatIndex | None
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """The two open-affix filters, resolved by text like everything else.

    "At least one open prefix" is a real filter on the trade site and a real source
    of value — an item with a free prefix is an item somebody can finish — and it is
    the one thing on the panel that is about what the item *does not* have. Returned
    with its own dropped-reason list because a filter that quietly vanishes turns a
    deliberate question into a wider one without saying so.
    """
    filters: list[dict[str, Any]] = []
    words: list[str] = []
    dropped: list[str] = []
    wanted = (
        (spec.open_prefixes, OPEN_PREFIX_TEXT, "open prefix"),
        (spec.open_suffixes, OPEN_SUFFIX_TEXT, "open suffix"),
    )
    for count, text, label in wanted:
        if count is None:
            continue
        stat_id = stats.stat_id(text, origin=_PSEUDO_GROUP) if stats is not None else None
        if stat_id is None:
            dropped.append(f"{label} count (no trade filter for {text!r})")
            continue
        filters.append({"id": stat_id, "disabled": False, "value": {"min": int(count)}})
        words.append(f"≥{int(count)} {label}{'' if count == 1 else 'es'}")
    return filters, words, dropped


def _resolve(
    mods: Sequence[ModFocus], stats: StatIndex | None, limit: int
) -> tuple[list[tuple[ModFocus, str]], list[str]]:
    resolved: list[tuple[ModFocus, str]] = []
    dropped: list[str] = []
    if stats is None:
        return resolved, [focus.text for focus in mods]
    for entry in mods:
        stat_id = stats.stat_id(entry.text, origin=entry.origin, local=entry.local)
        if stat_id is None:
            # Dropped rather than guessed: a wrong id returns nothing, which reads
            # as "worthless", and that is the one wrong answer this tier must not
            # give.
            _log.debug("no trade stat id for %r; dropping it from the query", entry.text)
            dropped.append(entry.text)
            continue
        resolved.append((entry, stat_id))
        if len(resolved) >= limit:
            break
    return resolved, dropped


def build_plan(
    item: NormalizedItem,
    stats: StatIndex | None,
    spec: QuerySpec | Sequence[ModFocus] | None = None,
    *,
    limit: int | None = None,
) -> tuple[QueryStep, ...]:
    """The searches this item is willing to make, widest last.

    Step 1 is the question the caller actually asked: *this base type, in this rarity,
    with these mods, rolled about this well, with this many affix slots still free.*
    Since Phase 9 the mods in it come from a **player's selection** rather than from a
    gate's opinion — that is the whole pivot, and it is why this function takes a
    :class:`~modules.prices.backend.api.QuerySpec` instead of deriving one.

    Step 2 exists only when :attr:`QuerySpec.broaden` is set, and only for the
    automatic path: it keeps the single most significant filter and drops the roll
    floor, which is the smallest broadening that can change a zero. A **manual** check
    never broadens, because silently widening the query answers a question the player
    did not ask and reports the answer under their heading.

    There is no step 3. A bare base-type search is not a price for a rare; it is the
    price of the cheapest junk sharing its base. The one exception is an item with no
    resolvable mods at all, where base type genuinely is everything the tool knows —
    that query is made, and :attr:`QueryStep.description` says so.

    Uniques skip the ladder: name plus base type is exact.
    """
    if item.rarity is Rarity.UNIQUE and item.name:
        body = _query_body(item, [])
        body["query"]["name"] = item.name
        return (QueryStep(body, f"{item.name} · {item.base_type}".strip(" ·")),)

    if spec is None:
        plan = QuerySpec(
            mods=tuple(significant_mods(item, limit or MAX_STAT_FILTERS)), broaden=True
        )
    elif isinstance(spec, QuerySpec):
        plan = spec
    else:
        plan = QuerySpec(mods=tuple(spec), broaden=True)

    cap = limit if limit is not None else plan.limit
    resolved, dropped = _resolve(plan.mods, stats, cap)
    affix_filters, affix_words, affix_dropped = _open_affix_filters(plan, stats)
    dropped = [*dropped, *affix_dropped]

    base = _describe_base(item)
    if not resolved and not affix_filters:
        body = _query_body(item, [])
        return (QueryStep(body, f"{base} · base type only — no mod resolved to a filter"),)

    entries = [_stat_filter(entry, stat_id) for entry, stat_id in resolved]
    words = [*(pair[1] for pair in entries), *affix_words]
    if dropped:
        words.append(f"({len(dropped)} not asked: {', '.join(dropped)})")
    step1 = QueryStep(
        _query_body(item, [*(pair[0] for pair in entries), *affix_filters]),
        f"{base} · " + " · ".join(words),
        tuple(words),
    )
    if not plan.broaden or not resolved:
        return (step1,)

    # The broadening: one filter, no floor. Same shape as step 1 so a reader can see
    # what was given up rather than diffing two hand-built dicts.
    widest, widest_stat_id = resolved[0]
    loose = ModFocus(
        text=widest.text,
        minimum=None,
        label=widest.label,
        origin=widest.origin,
        local=widest.local,
    )
    entry, phrase = _stat_filter(loose, widest_stat_id)
    step2 = QueryStep(
        _query_body(item, [entry]),
        f"{base} · {phrase} — broadened after no matches",
        (phrase,),
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
    spec: QuerySpec | Sequence[ModFocus] | None = None,
) -> dict[str, Any]:
    """The POST body for one item — the first step of :func:`build_plan`."""
    return build_plan(item, stats, spec)[0].body


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
        spec: QuerySpec | Sequence[ModFocus] | None = None,
        retry_on_empty: bool = True,
    ) -> TradeQuote:
        """One on-demand price. Two requests normally; three if the first search
        matched nothing and the broadening retry ran.

        A zero-result search is returned as a quote with ``chaos is None`` and
        ``total == 0`` — an *answer*, not an absence. The caller needs it that way to
        say "no matching listings" instead of "pricing…" forever.
        """
        stats: StatIndex | None = None
        if item.rarity is not Rarity.UNIQUE and (item.mods.total or _wants_affix_filter(spec)):
            # Deliberately not caught: a rare searched without stat filters is a
            # base-type search, which is too wide to be a price. `TradeUnavailable`
            # is a better answer than a misleading number.
            stats = await self.stats()
        plan = build_plan(item, stats, spec)
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


def _wants_affix_filter(spec: QuerySpec | Sequence[ModFocus] | None) -> bool:
    """Whether the stat index is needed even for an item with no readable mods.

    An unidentified rare has no mod lines and an open-affix filter is still a real
    question about it, so "no mods" is not the same as "nothing to resolve".
    """
    return isinstance(spec, QuerySpec) and (
        spec.open_prefixes is not None or spec.open_suffixes is not None
    )


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
