"""Tier 1 — poe.ninja's economy overviews (SPEC §5.1).

## The routes, corrected

SPEC §5.1 recorded, from research rather than measurement, that the legacy
``/api/data/currencyoverview`` paths 404 and that current routes look like
``/{poe1|poe2}/api/economy/{exchange|stash}/current/...``. Half right. Measured
2026-08-10 against the live site, and cross-checked against poe.ninja's own published
API reference at ``https://poe.ninja/docs/api``:

    GET /poe1/api/economy/leagues
    GET /poe1/api/economy/exchange/current/overview?league={league}&type={type}
    GET /poe1/api/economy/stash/current/item/overview?league={league}&type={type}
    GET /poe1/api/economy/stash/current/currency/overview?league={league}&type={type}

The league and category are **query parameters**, not path segments, and the path
ends in ``/overview``. The legacy 404 is confirmed. See ``research-notes.md`` §9.

## Two shapes, one unit

*Exchange* overviews (currency, fragments, scarabs, fossils, essences, oils, delirium
orbs, divination cards) return ``lines[]`` keyed by an opaque ``id`` with the name in
a sibling ``items[]`` array, and quote ``primaryValue`` in whatever ``core.primary``
says — ``chaos`` for PoE 1.

*Stash item* overviews (incubators, maps, uniques) return ``lines[]`` with ``name``,
``baseType``, ``chaosValue`` and ``divineValue`` inline.

They agree on the unit, and measurably so: every item line's ``chaosValue /
divineValue`` equals the exchange overview's Divine Orb rate to four figures, in both
Standard and the current challenge league. That is why this module ignores the
*stash currency* overview entirely — its ``chaosEquivalent`` is bulk-listing based and
disagrees with both (Divine Orb at 618c against the exchange's 898c on the same day).
Mixing the two would put two different chaoses in one total.

## The exchange ``id`` is the trade id

``lines[].id`` is ``divine``, ``chaos``, ``divination-scarab-of-pilfering`` — exactly
the tokens a player's ``~price 2 divine`` note uses. Tier 0 resolves against this
index directly, with no name table in between (see :mod:`.notes`).

## Politeness

poe.ninja publishes no ``X-Rate-Limit-*`` headers; it asks in prose instead. What it
serves is ``Cache-Control: public, max-age=1800`` with a weak ``ETag``, and its docs
say PoE 1 overviews refresh roughly every 15 minutes. So: conditional requests, a
30-minute TTL, a prefetch at module start, and never a fetch on a user action. It is
a different host from GGG, so it also costs zero of the account's request budget —
``net`` gives foreign hosts their own bucket (see ``modules/net/backend/client.py``).
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from modules.net.backend.api import NetApi, Response
from modules.prices.backend.api import PricesError
from runtime.log import get_logger
from runtime.storage import Storage

__all__ = [
    "CANDIDATES",
    "CATALOGUE",
    "DEFAULT_TTL",
    "EXCHANGE_PATH",
    "IRREGULAR_SLUGS",
    "ITEM_PATH",
    "LEAGUES_PATH",
    "NEVER_PREFETCH",
    "NINJA_BASE_URL",
    "NINJA_ROUTE",
    "PREFETCH",
    "SITEMAP_PATH",
    "NinjaCategory",
    "NinjaClient",
    "PriceLine",
    "PriceTable",
    "TableStore",
    "key_for_type",
    "parse_exchange",
    "parse_item_overview",
    "slug_to_type",
    "types_from_sitemap",
]

_log = get_logger("module.prices.ninja")

NINJA_BASE_URL = "https://poe.ninja"
LEAGUES_PATH = "/poe1/api/economy/leagues"
EXCHANGE_PATH = "/poe1/api/economy/exchange/current/overview"
ITEM_PATH = "/poe1/api/economy/stash/current/item/overview"
SITEMAP_PATH = "/sitemap.xml"
"""The only machine-readable list of *categories* poe.ninja publishes.

There is no per-league type index — measured 2026-08-10, ``/poe1/api/economy/types``,
``…/exchange/current/types``, ``…/index`` and ``/poe1/api/economy/categories`` all
404, and the only sibling of ``/leagues`` that answers is ``/leagues`` itself. The
sitemap is the fallback: it carries one ``/poe1/economy/{league}/{slug}`` URL per
category per league, so a mechanic that gets a page gets a slug, whether or not
anybody has typed its ``type`` name into :data:`CATALOGUE`. See
:func:`types_from_sitemap`."""

NINJA_ROUTE = "poe.ninja"
"""One rate-limit route for every poe.ninja call. `net` maps it to a ``host:``
policy of its own, so these requests can neither spend nor be blocked by GGG's."""

DEFAULT_TTL = 1800.0
"""Thirty minutes. What the server's own ``max-age`` says, and twice its refresh
interval — polling faster spends someone else's bandwidth for identical bytes."""

PRIMARY_CURRENCY = "chaos"

CACHE_VERSION = 1


@dataclass(frozen=True, slots=True)
class NinjaCategory:
    """One overview table: where to get it and what we call it."""

    key: str
    """Our name for it, and the key everything else uses."""

    kind: str
    """``exchange`` or ``item`` — which response shape to expect."""

    type: str
    """poe.ninja's ``type`` query value."""

    label: str

    @property
    def path(self) -> str:
        return EXCHANGE_PATH if self.kind == "exchange" else ITEM_PATH


def _cat(key: str, kind: str, type_: str, label: str) -> NinjaCategory:
    return NinjaCategory(key=key, kind=kind, type=type_, label=label)


def key_for_type(type_: str) -> str:
    """``UniqueAccessory`` → ``unique_accessory``. The catalogue key for a type name.

    Used for types this file has never heard of: discovery can turn a slug it found
    in the sitemap into a working category without anybody adding a line here.
    """
    out: list[str] = []
    for index, char in enumerate(type_):
        if char.isupper() and index and not type_[index - 1].isupper():
            out.append("_")
        out.append(char.lower())
    return "".join(out)


# Every table this module knows how to read — **all 44 types poe.ninja documents**
# (`https://poe.ninja/docs/api`, cross-checked against the 44 category slugs in its
# sitemap, 2026-08-10). It used to be 26, hand-typed, and the eighteen missing ones
# are the reason a bag full of ducats came back unpriceable: `Ducat` has been a live
# exchange type all along, with eleven priced lines in Allflame, and this module
# simply never asked for it. That is what :mod:`.discovery` exists to stop
# happening again — a hardcoded list is a list that goes stale between leagues.
CATALOGUE: dict[str, NinjaCategory] = {
    c.key: c
    for c in (
        # -- exchange overviews (18) --
        _cat("currency", "exchange", "Currency", "Currency"),
        _cat("fragment", "exchange", "Fragment", "Fragments"),
        _cat("scarab", "exchange", "Scarab", "Scarabs"),
        _cat("fossil", "exchange", "Fossil", "Fossils"),
        _cat("resonator", "exchange", "Resonator", "Resonators"),
        _cat("essence", "exchange", "Essence", "Essences"),
        _cat("oil", "exchange", "Oil", "Oils"),
        _cat("delirium_orb", "exchange", "DeliriumOrb", "Delirium Orbs"),
        _cat("card", "exchange", "DivinationCard", "Divination Cards"),
        _cat("artifact", "exchange", "Artifact", "Artifacts"),
        _cat("omen", "exchange", "Omen", "Omens"),
        _cat("tattoo", "exchange", "Tattoo", "Tattoos"),
        _cat("allflame_ember", "exchange", "AllflameEmber", "Allflame Embers"),
        _cat("runegraft", "exchange", "Runegraft", "Runegrafts"),
        _cat("ducat", "exchange", "Ducat", "Ducats"),
        _cat("djinn_coin", "exchange", "DjinnCoin", "Djinn Coins"),
        _cat("enshrouding_crystal", "exchange", "EnshroudingCrystal", "Enshrouding Crystals"),
        _cat("astrolabe", "exchange", "Astrolabe", "Astrolabes"),
        # -- stash item overviews (26) --
        _cat("incubator", "item", "Incubator", "Incubators"),
        _cat("map", "item", "Map", "Maps"),
        _cat("unique_map", "item", "UniqueMap", "Unique Maps"),
        _cat("blighted_map", "item", "BlightedMap", "Blighted Maps"),
        _cat("blight_ravaged_map", "item", "BlightRavagedMap", "Blight-Ravaged Maps"),
        _cat("valdo_map", "item", "ValdoMap", "Valdo's Maps"),
        _cat("unique_weapon", "item", "UniqueWeapon", "Unique Weapons"),
        _cat("unique_armour", "item", "UniqueArmour", "Unique Armours"),
        _cat("unique_accessory", "item", "UniqueAccessory", "Unique Accessories"),
        _cat("unique_flask", "item", "UniqueFlask", "Unique Flasks"),
        _cat("unique_jewel", "item", "UniqueJewel", "Unique Jewels"),
        _cat("unique_tincture", "item", "UniqueTincture", "Unique Tinctures"),
        _cat("unique_relic", "item", "UniqueRelic", "Unique Relics"),
        _cat("forbidden_jewel", "item", "ForbiddenJewel", "Forbidden Jewels"),
        _cat("cluster_jewel", "item", "ClusterJewel", "Cluster Jewels"),
        _cat("shrine_belt", "item", "ShrineBelt", "Shrine Belts"),
        _cat("wombgift", "item", "Wombgift", "Wombgifts"),
        _cat("skill_gem", "item", "SkillGem", "Skill Gems"),
        _cat("imbued_gem", "item", "ImbuedGem", "Imbued Gems"),
        _cat("invitation", "item", "Invitation", "Invitations"),
        _cat("memory", "item", "Memory", "Memories"),
        _cat("incursion_temple", "item", "IncursionTemple", "Incursion Temples"),
        _cat("base_type", "item", "BaseType", "Base Types"),
        _cat("flask", "item", "Flask", "Flasks"),
        _cat("beast", "item", "Beast", "Beasts"),
        _cat("vial", "item", "Vial", "Vials"),
    )
}

NEVER_PREFETCH: dict[str, str] = {
    "skill_gem": "4.0 MB, 7,508 lines; needs level/quality/corruption matching we do not do",
    "imbued_gem": "2.4 MB; the same variant problem as skill gems",
    "base_type": "9.4 MB, 20,165 lines; priced per ilvl and influence, which we cannot match",
    "valdo_map": "1.6 MB, 1,509 variant lines; a wrong variant is a wrong order of magnitude",
    "cluster_jewel": "850 lines keyed by enchantment; `choose_line` does not score variants",
    "forbidden_jewel": "330 lines keyed by ascendancy notable; same variant problem",
}
"""Types discovery must never add to the refresh set, and why.

Sizes measured against Allflame and Standard on 2026-08-10. Every entry is either
too large to fetch on a 30-minute cycle or priced by a variant field
:func:`~modules.prices.backend.valuation.choose_line` does not read — and an item
priced as the wrong variant is worse than an item left unpriced, which is the same
rule that has kept skill gems out since Phase 3."""

CANDIDATES: tuple[str, ...] = tuple(
    key for key in CATALOGUE if key not in NEVER_PREFETCH
)
"""What :mod:`.discovery` probes for a league. 38 of the 44 documented types; the
other six are :data:`NEVER_PREFETCH`. Measured cost of one full pass: 3.4 MB for
Allflame, 4.0 MB for Standard, against 3.0 MB for the old sixteen-table prefetch —
and every byte of it is a table we keep, so the pass *is* the refresh."""

BY_TYPE: dict[str, NinjaCategory] = {c.type.casefold(): c for c in CATALOGUE.values()}
"""``type`` → category, because our keys are not always the derived name.

``DivinationCard`` has been keyed ``card`` since Phase 3. Matching a discovered type
on the *key* would therefore have produced a second, duplicate ``divination_card``
category and fetched the same 116 kB table twice — which is what the fixture server
caught the first time this ran."""

IRREGULAR_SLUGS: dict[str, str] = {
    "temples": "IncursionTemple",
}
"""Sitemap slugs :func:`slug_to_type` cannot derive. Exactly one, of 44."""

PREFETCH: tuple[str, ...] = (
    "currency",
    "fragment",
    "scarab",
    "fossil",
    "essence",
    "oil",
    "delirium_orb",
    "card",
    "incubator",
    "map",
    "unique_map",
    "unique_weapon",
    "unique_armour",
    "unique_accessory",
    "unique_flask",
    "unique_jewel",
)
"""The static fallback: the sixteen tables SPEC §5.1 named, used only when
:mod:`.discovery` is switched off or could not run.

It is *not* the normal path any more, and it should not be read as a list of what
exists. It omitted ``Ducat`` for a whole league. What a league serves is discovered;
this is what to hold when the league could not be asked."""


@dataclass(frozen=True, slots=True)
class PriceLine:
    """One priced row, normalized across the two response shapes."""

    name: str
    chaos: float
    category: str
    trade_id: str | None = None
    """Present on exchange lines. The token a ``~price N <id>`` note uses."""

    base_type: str | None = None
    variant: str | None = None
    links: int | None = None
    corrupted: bool | None = None
    listing_count: int = 0
    count: int = 0

    def detail(self) -> str:
        """A short human description of *which* line this is, for a detail pane."""
        parts: list[str] = []
        if self.base_type and self.base_type != self.name:
            parts.append(self.base_type)
        if self.links:
            parts.append(f"{self.links}L")
        if self.variant:
            parts.append(self.variant.lstrip(", "))
        if self.corrupted:
            parts.append("corrupted")
        return ", ".join(parts)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "chaos": self.chaos,
            "trade_id": self.trade_id,
            "base_type": self.base_type,
            "variant": self.variant,
            "links": self.links,
            "corrupted": self.corrupted,
            "listing_count": self.listing_count,
            "count": self.count,
        }

    @classmethod
    def from_json(cls, category: str, data: Mapping[str, Any]) -> PriceLine:
        return cls(
            name=str(data.get("name") or ""),
            chaos=float(data.get("chaos") or 0.0),
            category=category,
            trade_id=data.get("trade_id"),
            base_type=data.get("base_type"),
            variant=data.get("variant"),
            links=data.get("links"),
            corrupted=data.get("corrupted"),
            listing_count=int(data.get("listing_count") or 0),
            count=int(data.get("count") or 0),
        )


@dataclass
class PriceTable:
    """One category's lines, indexed, with the freshness that came with them."""

    category: str
    league: str
    lines: list[PriceLine]
    fetched_at: float
    """Wall-clock epoch seconds, so age survives a restart."""

    etag: str | None = None
    by_name: dict[str, list[PriceLine]] = field(default_factory=dict, repr=False)
    by_trade_id: dict[str, PriceLine] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for line in self.lines:
            if not line.name:
                continue
            self.by_name.setdefault(line.name.casefold(), []).append(line)
            if line.trade_id:
                # Duplicate ids do not occur; if one ever did, the first wins and
                # the table is still usable.
                self.by_trade_id.setdefault(line.trade_id.casefold(), line)

    def age(self, now: float) -> float:
        return max(0.0, now - self.fetched_at)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "category": self.category,
            "league": self.league,
            "fetched_at": self.fetched_at,
            "etag": self.etag,
            "lines": [line.to_json() for line in self.lines],
        }

    @classmethod
    def from_json(cls, data: Any) -> PriceTable | None:
        if not isinstance(data, Mapping) or data.get("version") != CACHE_VERSION:
            return None
        raw_lines = data.get("lines")
        fetched_at = data.get("fetched_at")
        category = data.get("category")
        if not isinstance(raw_lines, list) or not isinstance(fetched_at, (int, float)):
            return None
        if not isinstance(category, str) or not isinstance(data.get("league"), str):
            return None
        return cls(
            category=category,
            league=str(data["league"]),
            lines=[
                PriceLine.from_json(category, entry)
                for entry in raw_lines
                if isinstance(entry, Mapping)
            ],
            fetched_at=float(fetched_at),
            etag=data.get("etag") if isinstance(data.get("etag"), str) else None,
        )


# -- the sitemap, and the slugs in it ---------------------------------------------


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("sses") or word.endswith("shes") or word.endswith("ches"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def slug_to_type(slug: str) -> str | None:
    """``unique-accessories`` → ``UniqueAccessory``. The API ``type`` for a page slug.

    De-pluralize the last word, then PascalCase. Checked against all 44 slugs in
    poe.ninja's sitemap on 2026-08-10: it derives 43 of them, and the one exception
    (``temples`` → ``IncursionTemple``) is in :data:`IRREGULAR_SLUGS`. ``None`` for a
    slug that is empty or not a category.

    This function is the whole reason discovery can find a type nobody has typed
    into :data:`CATALOGUE`. A rule that only recognised known names would have
    reproduced the ``Ducat`` bug exactly.
    """
    slug = slug.strip().strip("/").casefold()
    if not slug or "/" in slug:
        return None
    known = IRREGULAR_SLUGS.get(slug)
    if known:
        return known
    parts = [part for part in slug.split("-") if part]
    if not parts:
        return None
    parts[-1] = _singular(parts[-1])
    return "".join(part[:1].upper() + part[1:] for part in parts)


def types_from_sitemap(xml: str, league: str) -> list[str]:
    """Category slugs poe.ninja publishes a page for, in ``league``.

    Parsed with a regex rather than an XML parser on purpose: the document is 1,100+
    URLs of a shape we have measured, we want exactly one path segment out of it, and
    an XML parser on a network-supplied document is a larger attack surface than the
    job justifies.

    The league appears in the URL lowercased and space-stripped
    (``Hardcore Allflame`` → ``allflamehc``… and that is *not* derivable, so both the
    obvious form and a space-free form are accepted, and an empty answer is a normal
    outcome the caller falls back from).
    """
    wanted = {
        league.casefold().replace(" ", "-"),
        league.casefold().replace(" ", ""),
    }
    slugs: list[str] = []
    seen: set[str] = set()
    for match in _SITEMAP_URL.finditer(xml):
        if match.group("league").casefold() not in wanted:
            continue
        slug = match.group("slug").casefold()
        if slug and slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    return slugs


_SITEMAP_URL = re.compile(
    r"poe\.ninja/poe1/economy/(?P<league>[^/<\s]+)/(?P<slug>[a-z0-9-]+)(?=[/<\s]|$)"
)


# -- parsing --------------------------------------------------------------------


def _names_from(payload: Mapping[str, Any]) -> dict[str, str]:
    """``id -> name`` for an exchange overview.

    The top-level ``items`` array covers the priced lines; ``core.items`` covers the
    reference currencies. Both are read, top level last so it wins.
    """
    names: dict[str, str] = {}
    core = payload.get("core")
    for source in (core.get("items") if isinstance(core, Mapping) else None, payload.get("items")):
        if not isinstance(source, Sequence) or isinstance(source, (str, bytes)):
            continue
        for entry in source:
            if not isinstance(entry, Mapping):
                continue
            key, name = entry.get("id"), entry.get("name")
            if isinstance(key, str) and isinstance(name, str) and name:
                names[key] = name
    return names


def parse_exchange(payload: Any, category: str) -> list[PriceLine]:
    """A currency-exchange overview → :class:`PriceLine` list.

    Raises :class:`PricesError` if the reference currency is not chaos, rather than
    silently emitting numbers in a unit nothing else in this module uses.
    """
    if not isinstance(payload, Mapping):
        raise PricesError(f"{category}: exchange overview was not an object")
    core = payload.get("core")
    primary = core.get("primary") if isinstance(core, Mapping) else None
    if primary is not None and str(primary).casefold() != PRIMARY_CURRENCY:
        raise PricesError(
            f"{category}: exchange overview is quoted in {primary!r}, not chaos"
        )
    names = _names_from(payload)
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, Sequence) or isinstance(raw_lines, (str, bytes)):
        return []
    out: list[PriceLine] = []
    for entry in raw_lines:
        if not isinstance(entry, Mapping):
            continue
        trade_id = entry.get("id")
        value = entry.get("primaryValue")
        if not isinstance(trade_id, str) or not _is_number(value) or float(value) <= 0:
            continue
        out.append(
            PriceLine(
                name=names.get(trade_id, trade_id),
                chaos=float(value),
                category=category,
                trade_id=trade_id,
                listing_count=_int(entry.get("volumePrimaryValue")),
            )
        )
    return out


def parse_item_overview(payload: Any, category: str) -> list[PriceLine]:
    """A stash item overview → :class:`PriceLine` list."""
    if not isinstance(payload, Mapping):
        raise PricesError(f"{category}: item overview was not an object")
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, Sequence) or isinstance(raw_lines, (str, bytes)):
        return []
    out: list[PriceLine] = []
    for entry in raw_lines:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        value = entry.get("chaosValue")
        if not isinstance(name, str) or not name or not _is_number(value) or float(value) <= 0:
            continue
        tier = entry.get("mapTier")
        variant = entry.get("variant")
        # `mapTier` is documented but absent from every league sampled on 2026-08-10:
        # modern map lines carry the tier in the name ("Map (Tier 16)") instead. Read
        # it when present, and let the name-based key carry the rest.
        if isinstance(tier, int) and not isinstance(tier, bool) and f"Tier {tier}" not in name:
            name = f"{name} (Tier {tier})"
        out.append(
            PriceLine(
                name=name,
                chaos=float(value),
                category=category,
                base_type=entry.get("baseType") if isinstance(entry.get("baseType"), str) else None,
                variant=variant if isinstance(variant, str) else None,
                links=_optional_int(entry.get("links")),
                corrupted=(
                    bool(entry["corrupted"]) if isinstance(entry.get("corrupted"), bool) else None
                ),
                listing_count=_int(entry.get("listingCount")),
                count=_int(entry.get("count")),
            )
        )
    return out


def parse_table(payload: Any, category: NinjaCategory, league: str, now: float,
                etag: str | None) -> PriceTable:
    lines = (
        parse_exchange(payload, category.key)
        if category.kind == "exchange"
        else parse_item_overview(payload, category.key)
    )
    return PriceTable(
        category=category.key, league=league, lines=lines, fetched_at=now, etag=etag
    )


# -- storage ----------------------------------------------------------------------


class TableStore:
    """Parsed tables on disk, one file per (league, category).

    The *parsed* lines are stored rather than the raw payload — the opposite choice
    from ``poeapi``'s response cache, and for a concrete reason: the unique-weapon
    overview is 900 kB of mod text and flavour text on the wire and about 40 kB once
    reduced to the six fields pricing reads. ``CACHE_VERSION`` guards the shape.
    """

    def __init__(self, storage: Storage, *, clock: Callable[[], float] | None = None) -> None:
        self._storage = storage
        self._clock = clock or time.time

    def now(self) -> float:
        return self._clock()

    @staticmethod
    def filename(league: str, category: str) -> str:
        digest = hashlib.sha1(f"{league}:{category}".encode()).hexdigest()
        return f"table-{digest}.json"

    def load(self, league: str, category: str) -> PriceTable | None:
        try:
            data = self._storage.read_json(self.filename(league, category))
        except Exception as exc:  # a corrupt cache file must never be fatal
            _log.warning("discarding unreadable price table: %s", exc)
            return None
        table = PriceTable.from_json(data)
        if table is None or table.league != league or table.category != category:
            return None
        return table

    def save(self, table: PriceTable) -> None:
        self._storage.write_json(
            self.filename(table.league, table.category), table.to_json()
        )


# -- the client -------------------------------------------------------------------


class NinjaClient:
    """Fetches overviews through `net`. Holds no state beyond its configuration."""

    def __init__(
        self,
        net: NetApi,
        *,
        base_url: str = NINJA_BASE_URL,
        route: str = NINJA_ROUTE,
        timeout: float = 20.0,
    ) -> None:
        self._net = net
        self._base_url = base_url.rstrip("/")
        self._route = route
        self._timeout = timeout

    def url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def leagues(self) -> list[str]:
        """Active economy league ids, current challenge league first."""
        response = await self._get(LEAGUES_PATH)
        payload = response.json()
        if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
            raise PricesError("poe.ninja returned no league list")
        out: list[str] = []
        for entry in payload:
            if isinstance(entry, Mapping) and isinstance(entry.get("id"), str):
                out.append(entry["id"])
        return out

    async def sitemap_slugs(self, league: str) -> list[str]:
        """Category slugs poe.ninja publishes for ``league``. ``[]`` if it cannot say.

        One request, ~200 kB, made once per league per discovery window. Never fatal:
        the sitemap is a *supplement* to :data:`CATALOGUE`, so a 500 here costs the
        eighteen types nobody had typed in, not the thirty-eight that are.
        """
        response = await self._get(SITEMAP_PATH)
        return types_from_sitemap(response.text, league)

    async def fetch(
        self,
        category: NinjaCategory,
        league: str,
        *,
        etag: str | None = None,
        now: float | None = None,
    ) -> PriceTable | None:
        """One overview, or ``None`` when the server answered 304.

        ``None`` is not a failure: it is the ETag doing its job, and the caller keeps
        the copy it already has.
        """
        headers = {"If-None-Match": etag} if etag else None
        response = await self._get(
            category.path, params={"league": league, "type": category.type}, headers=headers
        )
        if response.not_modified:
            return None
        return parse_table(
            response.json(),
            category,
            league,
            time.time() if now is None else now,
            response.etag,
        )

    async def _get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        return await self._net.get(
            self.url(path),
            params=params,
            headers=headers,
            # poe.ninja must never see the account credential. `net` enforces this by
            # host as well; saying it here makes the intent local.
            authenticated=False,
            route=self._route,
            timeout=self._timeout,
        )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int(value: Any, default: int = 0) -> int:
    return int(value) if _is_number(value) else default


def _optional_int(value: Any) -> int | None:
    return int(value) if _is_number(value) else None
