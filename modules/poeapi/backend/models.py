"""The normalized data model (SPEC §4.5).

These pydantic models are **the single source of truth for the TypeScript types**
generated later (IMPLEMENTATION-PLAN §3), so every field here is JSON-serializable
and every name is the name the frontend will use. Nothing in this file knows how the
API spells things; :mod:`modules.poeapi.backend.normalize` owns that translation.

Two design choices worth stating:

* **Every response model carries its own freshness.** ``fetched_at``, ``from_cache``,
  ``stale`` and ``retry_after`` travel with the data rather than beside it, because
  the honest sync states of Phase 5 (fresh / stale / syncing / unchanged / error /
  restricted) are only expressible if the payload itself says which one it is.
* **``content_hash`` is computed over the normalized items**, never over raw JSON
  (SPEC §4.4). Raw JSON contains fields that churn without the inventory changing;
  hashing it would report a change every sync and defeat the point.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Character",
    "CharacterList",
    "Grid",
    "ItemSet",
    "Location",
    "Mods",
    "NormalizedItem",
    "Rarity",
    "Sockets",
    "Source",
    "StashTab",
    "StashTabList",
    "utcnow",
]


def utcnow() -> datetime:
    return datetime.now(UTC)


class Rarity(StrEnum):
    """A 1:1 reading of GGG's ``frameType``.

    Deliberately not collapsed to normal/magic/rare/unique. ``frameType`` 5 is not
    "a normal item that happens to be currency"; flattening it would force every
    consumer to re-derive the distinction from the base type, which is exactly the
    string matching the normalized model exists to abolish.
    """

    NORMAL = "normal"
    MAGIC = "magic"
    RARE = "rare"
    UNIQUE = "unique"
    GEM = "gem"
    CURRENCY = "currency"
    DIVINATION = "divination"
    QUEST = "quest"
    PROPHECY = "prophecy"
    RELIC = "relic"
    UNKNOWN = "unknown"


class Source(StrEnum):
    BAG = "bag"
    """``MainInventory`` — the backpack. This is what the appraisal screen is about."""

    EQUIPMENT = "equipment"
    """Worn gear and flasks. Fetched by the same call; never part of the bag total."""

    STASH = "stash"


class Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Grid(Base):
    """Slot-accurate placement, so the 12x5 bag grid can be drawn (SPEC §4.5)."""

    x: int = 0
    y: int = 0
    w: int = 1
    h: int = 1


class Sockets(Base):
    count: int = 0
    links: int = 0
    """Size of the largest link group, not the number of groups."""

    colors: list[str] = Field(default_factory=list)
    """One entry per socket, in the API's order: ``R``/``G``/``B``/``W``/``A``/``DV``."""


class Mods(Base):
    """Mod text by origin. Strings, because that is what the API gives and what a
    human reads; structured stat ids are a trade-API concern and belong to Phase 3."""

    implicit: list[str] = Field(default_factory=list)
    explicit: list[str] = Field(default_factory=list)
    crafted: list[str] = Field(default_factory=list)
    enchant: list[str] = Field(default_factory=list)
    fractured: list[str] = Field(default_factory=list)
    utility: list[str] = Field(default_factory=list)
    """Flask utility mods. Not in SPEC §4.5's list, but a flask with none of its mods
    recorded is indistinguishable from a white flask, and that misprices it."""

    veiled: list[str] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(
            len(group)
            for group in (
                self.implicit,
                self.explicit,
                self.crafted,
                self.enchant,
                self.fractured,
                self.utility,
                self.veiled,
            )
        )


class Location(Base):
    source: Source
    tab_id: str | None = None
    tab_name: str | None = None
    tab_index: int | None = None
    slot: str | None = None
    """The raw ``inventoryId`` (``Weapon``, ``Ring2``, ``MainInventory``, …). Kept
    because "which finger" is not derivable from anything else in the model."""


class NormalizedItem(Base):
    """The boundary (SPEC §4.5). Pricing consumes this, never raw API JSON."""

    uid: str
    name: str
    base_type: str
    category: str
    subcategory: str | None = None
    rarity: Rarity = Rarity.UNKNOWN
    ilvl: int = 0
    stack_size: int = 1
    max_stack_size: int | None = None
    map_tier: int | None = None
    """The ``Map Tier`` property, for maps only. Pricing needs it: poe.ninja indexes
    ordinary maps as ``Map (Tier 16)`` rather than by their area name (SPEC §5.1), so
    without the tier a map cannot be looked up at all."""

    grid: Grid = Grid()
    sockets: Sockets = Sockets()
    corrupted: bool = False
    fractured: bool = False
    synthesised: bool = False
    identified: bool = True
    influences: list[str] = Field(default_factory=list)
    mods: Mods = Mods()
    note: str | None = None
    """The user's own ``~price`` / ``~b/o`` tag. Tier 0 of the pricing engine
    (SPEC §5.0) and free with every fetch — Phase 3 depends on it being here."""

    location: Location
    icon: str | None = None

    def digest(self) -> str:
        """A stable fingerprint of everything that would change a price.

        Excludes ``icon`` (a CDN URL that carries a rotating cache-busting segment)
        and ``location`` (moving an item within a tab is not a change worth syncing
        for), so that a bag hash answers "is anything different?" rather than "did
        any byte move?".
        """
        payload = self.model_dump(mode="json", exclude={"icon", "location"})
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class Meta(Base):
    """Freshness, carried by every response model."""

    fetched_at: datetime
    from_cache: bool = False
    stale: bool = False
    """``True`` when this is cached data served because a live fetch was refused."""

    retry_after: float | None = None
    """Seconds until a live fetch would be allowed, when ``stale``."""

    note: str | None = None
    """Why it is stale, in words a UI can show."""


class ItemSet(Base):
    """A bag, an equipment set, or one stash tab, normalized."""

    items: list[NormalizedItem] = Field(default_factory=list)
    source: Source
    character: str | None = None

    league: str | None = None
    """Which economy these items belong to — the character's league for a bag, the
    tab's league for a stash tab.

    ``None`` means *unknown*, and consumers must treat it as unknown rather than as
    "Standard". Every price in the tool is denominated per league (a Divine Orb was
    897.7c in Standard and 209.0c in Allflame on the same measured day), so a bag
    that has lost this field cannot be priced at all — which is the correct outcome,
    and better than the confident wrong total it used to produce.
    """

    tab_index: int | None = None
    tab_name: str | None = None
    meta: Meta

    @property
    def content_hash(self) -> str:
        """SHA-256 of the *normalized* item set (SPEC §4.4).

        Order-independent: the API is free to reorder items, and a reordering is not
        a change. Two syncs with the same hash mean nothing that matters moved.
        """
        digests = sorted(item.digest() for item in self.items)
        return hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()

    @property
    def total_stack(self) -> int:
        return sum(item.stack_size for item in self.items)

    def by_source(self, source: Source) -> list[NormalizedItem]:
        return [item for item in self.items if item.location.source is source]

    def to_json(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["content_hash"] = self.content_hash
        return data


class Character(Base):
    name: str
    league: str | None = None
    realm: str | None = None
    """Which realm the account lives on — ``pc``, ``xbox`` or ``sony``.

    Every character-window request takes a ``realm`` parameter, and this entry is
    the only place the answer is published. It used to be a module constant reading
    ``"pc"``, which is the league bug in miniature: a plausible default silently
    answering a question only the account can answer."""

    class_name: str | None = None
    level: int = 0
    experience: int = 0
    current: bool = False
    """GGG marks the most recently played character. The one to sync by default."""


class CharacterList(Base):
    characters: list[Character] = Field(default_factory=list)
    meta: Meta

    def current(self) -> Character | None:
        for character in self.characters:
            if character.current:
                return character
        return self.characters[0] if self.characters else None

    def named(self, name: str) -> Character | None:
        lowered = name.casefold()
        for character in self.characters:
            if character.name.casefold() == lowered:
                return character
        return None

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class StashTab(Base):
    index: int
    id: str | None = None
    name: str = ""
    type: str = ""
    colour: str | None = None
    hidden: bool = False
    remove_only: bool = False
    """Remove-only tabs can never gain items, so they can be fetched once and cached
    forever (research-notes §7). 86% of the measured Standard stash is remove-only."""

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class StashTabList(Base):
    league: str
    tabs: list[StashTab] = Field(default_factory=list)
    meta: Meta

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
