"""Which poe.ninja tables actually exist for a league — asked, not assumed.

## The bug this file is the fix for

``CATALOGUE`` used to be twenty-six types, typed in by hand. ``Ducat`` was not one
of them. It had been a live exchange type all along — eleven priced lines in
Allflame, its own page on poe.ninja — and because nobody had typed the word, every
ducat in a real bag came back ``unpriceable``. No amount of care with the *other*
twenty-six would have caught that, because the failure is not in how a table is
read; it is in the list of tables we thought to ask for.

A hardcoded list also goes stale in the other direction. ``AllflameEmber`` and
``Runegraft`` are this league's mechanics and are nearly empty in Standard;
``Incubator``, ``ShrineBelt`` and ``Memory`` serve zero lines in Allflame and are a
wasted request every thirty minutes. Measured 2026-08-10, both leagues, all 44
documented types.

So: **ask the league.**

## What discovery actually does

1. **Read poe.ninja's sitemap** for ``/poe1/economy/{league}/{slug}`` URLs. This is
   the only machine-readable index of categories the site publishes — there is no
   type endpoint, and §9.6 of ``research-notes.md`` records the four 404s that
   establish that. Each slug becomes a candidate ``type`` through
   :func:`~modules.prices.backend.ninja.slug_to_type`, which derives 43 of the 44
   by rule and looks the last one up in a two-entry table.
2. **Probe every candidate** — the 38 catalogue types that are not
   :data:`~modules.prices.backend.ninja.NEVER_PREFETCH`, plus any type the sitemap
   surfaced that the catalogue has never heard of. A probe *is* a fetch, so the
   pass costs nothing beyond the refresh it performs.
3. **Record what served data**, per league, on disk, for a day. Later refreshes
   fetch the served set only.

Step 1 is the part that matters and the part that is easy to get wrong. A discovery
that only validated the names already in the catalogue would have confirmed all
twenty-six and still missed ducats — it would have been a slower way to have the
same bug.

## What it cannot do

The sitemap lists the same 44 slugs for every league; it is an index of *categories*,
not of per-league availability. That is why step 2 exists. And a type poe.ninja
serves but never links a page for is invisible to this file — the fallback for that
case is the bulk exchange (:mod:`.exchange`), not discovery.

Failure is never fatal. No sitemap, no probe, a corrupt record: the module falls
back to :data:`~modules.prices.backend.ninja.PREFETCH`, the static sixteen, and says
so in :attr:`LeagueCatalogue.source`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from modules.prices.backend.ninja import (
    BY_TYPE,
    CATALOGUE,
    NinjaCategory,
    key_for_type,
    slug_to_type,
)
from runtime.log import get_logger
from runtime.storage import Storage

__all__ = [
    "DISCOVERY_TTL",
    "CatalogueStore",
    "LeagueCatalogue",
    "candidates_from_slugs",
]

_log = get_logger("module.prices.discovery")

DISCOVERY_TTL = 86400.0
"""One day. Which types a league serves changes when GGG ships a patch, not when the
hour turns, and the pass that refreshes it is the most expensive one this module
makes."""

DISCOVERY_VERSION = 1

STATIC = "static"
PROBED = "probed"


@dataclass
class LeagueCatalogue:
    """Which tables a league serves, and how we came to believe that."""

    league: str
    discovered_at: float
    served: list[str] = field(default_factory=list)
    """Catalogue keys that came back with at least one priced line."""

    empty: list[str] = field(default_factory=list)
    """Probed, answered 200, and had nothing in it. Skipped until the record
    expires — this is the saving discovery buys, and in Allflame it is four tables
    (``DjinnCoin``, ``Incubator``, ``Memory``, ``ShrineBelt``) every half hour."""

    failed: dict[str, str] = field(default_factory=dict)
    """Probed and errored. Retried on the next discovery, never treated as absent —
    a 500 is not evidence that a league has no scarabs."""

    found: dict[str, dict[str, str]] = field(default_factory=dict)
    """Categories the **sitemap** produced that :data:`CATALOGUE` did not contain,
    as ``key -> {kind, type, label}``. Empty against today's site, and the whole
    point of the mechanism: this is where next league's mechanic lands without a
    code change, carrying enough to be fetched on the next refresh."""

    unmapped: list[str] = field(default_factory=list)
    """Sitemap slugs no derivation turned into a type that answers. Kept so the
    failure is inspectable — a slug silently dropped here is the ``Ducat`` bug
    wearing a different hat."""

    source: str = STATIC
    """``probed`` when this record came from a live pass, ``static`` when discovery
    could not run and the caller is using the hardcoded fallback."""

    def age(self, now: float) -> float:
        return max(0.0, now - self.discovered_at)

    def fresh(self, now: float, ttl: float = DISCOVERY_TTL) -> bool:
        return self.source == PROBED and self.age(now) < ttl

    def category(self, key: str) -> NinjaCategory | None:
        """The category for a key, from the catalogue or from what discovery found."""
        known = CATALOGUE.get(key)
        if known is not None:
            return known
        spec = self.found.get(key)
        if not spec:
            return None
        return NinjaCategory(
            key=key,
            kind=spec.get("kind", "exchange"),
            type=spec.get("type", ""),
            label=spec.get("label", key),
        )

    def categories(self) -> list[NinjaCategory]:
        """Every served table, ready to fetch."""
        out = []
        for key in self.served:
            category = self.category(key)
            if category is not None:
                out.append(category)
        return out

    def describe(self) -> str:
        """One line for a status panel. Says where the *list* came from."""
        if self.source != PROBED:
            return (
                f"{len(self.served)} table(s) from the built-in list — the league was "
                "not asked (discovery off or unavailable)"
            )
        parts = [f"{len(self.served)} served"]
        if self.empty:
            parts.append(f"{len(self.empty)} empty in this league")
        if self.found:
            parts.append(f"{len(self.found)} not in the built-in catalogue")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        if self.unmapped:
            parts.append(f"{len(self.unmapped)} sitemap slug(s) unrecognised")
        return f"asked {self.league}: " + ", ".join(parts)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": DISCOVERY_VERSION,
            "league": self.league,
            "discovered_at": self.discovered_at,
            "served": list(self.served),
            "empty": list(self.empty),
            "failed": dict(self.failed),
            "found": {key: dict(spec) for key, spec in self.found.items()},
            "unmapped": list(self.unmapped),
            "source": self.source,
        }

    @classmethod
    def from_json(cls, data: Any) -> LeagueCatalogue | None:
        if not isinstance(data, Mapping) or data.get("version") != DISCOVERY_VERSION:
            return None
        league, when = data.get("league"), data.get("discovered_at")
        if not isinstance(league, str) or not isinstance(when, (int, float)):
            return None
        return cls(
            league=league,
            discovered_at=float(when),
            served=[str(k) for k in data.get("served") or []],
            empty=[str(k) for k in data.get("empty") or []],
            failed={
                str(k): str(v) for k, v in (data.get("failed") or {}).items()
            },
            found={
                str(key): {str(a): str(b) for a, b in (spec or {}).items()}
                for key, spec in (data.get("found") or {}).items()
                if isinstance(spec, Mapping)
            },
            unmapped=[str(s) for s in data.get("unmapped") or []],
            source=str(data.get("source") or STATIC),
        )


class CatalogueStore:
    """One discovery record per league, on disk. Tiny; keyed like the tables."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    @staticmethod
    def filename(league: str) -> str:
        digest = hashlib.sha1(f"catalogue:{league}".encode()).hexdigest()
        return f"catalogue-{digest}.json"

    def load(self, league: str) -> LeagueCatalogue | None:
        try:
            data = self._storage.read_json(self.filename(league))
        except Exception as exc:  # a corrupt cache must never be fatal
            _log.warning("discarding unreadable catalogue record: %s", exc)
            return None
        record = LeagueCatalogue.from_json(data)
        return record if record is not None and record.league == league else None

    def save(self, record: LeagueCatalogue) -> None:
        self._storage.write_json(self.filename(record.league), record.to_json())


def candidates_from_slugs(
    slugs: Sequence[str],
) -> tuple[list[NinjaCategory], list[str]]:
    """Sitemap slugs → categories the catalogue does not already carry.

    Returns ``(new_categories, unmapped_slugs)``. A slug whose derived type is
    already in :data:`CATALOGUE` produces neither: it is already being probed. The
    check is on the **type**, not on the derived key — ``DivinationCard`` is keyed
    ``card`` here, and matching on the key produced a duplicate table.

    The response *shape* of a type nobody has typed in is unknown, so the category
    is emitted with ``kind="exchange"`` and the caller is expected to retry it as
    ``item`` when the exchange path 404s. That two-request cost is paid once per
    league per day, and only for slugs that are genuinely new.
    """
    new: list[NinjaCategory] = []
    unmapped: list[str] = []
    seen: set[str] = set()
    for slug in slugs:
        type_ = slug_to_type(slug)
        if type_ is None:
            unmapped.append(slug)
            continue
        if type_.casefold() in BY_TYPE:
            continue
        key = key_for_type(type_)
        if key in CATALOGUE or key in seen:
            continue
        seen.add(key)
        new.append(
            NinjaCategory(
                key=key,
                kind="exchange",
                type=type_,
                label=slug.replace("-", " ").title(),
            )
        )
    return new, unmapped
