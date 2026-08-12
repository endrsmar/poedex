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
    "CHARACTER_ENV",
    "Budget",
    "Character",
    "CharacterChoice",
    "CharacterList",
    "CharacterSelection",
    "CharacterSource",
    "CrawlPlan",
    "CrawlProgress",
    "Grid",
    "ItemSet",
    "Location",
    "Mods",
    "NormalizedItem",
    "Profile",
    "Rarity",
    "Sockets",
    "Source",
    "StashState",
    "StashTab",
    "StashTabList",
    "TabKind",
    "TabLayout",
    "TabState",
    "format_last_login",
    "utcnow",
]

CHARACTER_ENV = "POEDEX_CHARACTER"
"""Which character to read, for the lifetime of one process.

Declared here rather than in ``api.py`` — which re-exports it, and is still where
dependents import it from — because :meth:`CharacterChoice.describe` names it in the
sentence it shows the player, and this module may not import the one above it.
"""


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


class Gem(Base):
    """A skill gem's level and quality — two of the three axes it is priced on.

    The third is :attr:`NormalizedItem.corrupted`, which every item already has. These
    two are here because a gem is the one thing in the bag whose *name is not enough*
    to look up: a level 21 / 20% Cyclone and a level 1 Cyclone share a name, share a
    base type, and are three orders of magnitude apart. Without them the honest answer
    is `unpriceable`, which is what `prices` returned for every gem until now — an
    unpriced gem is honest, a gem priced as the wrong variant is not.

    Alternate-quality gems are **not** a fourth axis: the game gives them their own
    names (``Blade Flurry of Incision``) and so does poe.ninja, so they match by name
    like everything else.
    """

    level: int | None = None
    """``None`` when the wire carried no readable ``Level`` property.

    Not defaulted to 1. A gem whose level cannot be read cannot be matched to a
    variant, and guessing the cheapest one is exactly how a level 21 gem gets priced
    as a level 1 — the failure the exclusion existed to prevent."""

    quality: int = 0
    """Absent means zero, which is GGG's own encoding: the ``Quality`` property is
    omitted on a 0% gem rather than sent as ``+0%``."""


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

    gem: Gem | None = None
    """Level and quality, for skill gems only. ``None`` says "not a gem" rather than
    "a gem nothing is known about", and `prices` needs to tell those apart."""

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


class CharacterSource(StrEnum):
    """Why a particular character is the one being read.

    Carried to every surface for the reason :class:`~modules.prices.backend.api.LeagueSource`
    is: a name on its own is not an answer a player can check. "PlaceholderHierophant, because
    you pinned it" and "PlaceholderHierophant, because it is the one you last played" are
    different claims, and exactly one of them is a bug when the player has just
    rolled a new character.

    Declared up here, away from the rest of its family, only because
    :class:`ItemSet` annotates a field with it and pydantic resolves annotations
    when the class is built.
    """

    ARGUMENT = "argument"
    """``--character``. The most explicit thing a caller can say."""

    ENVIRONMENT = "environment"
    """``POEDEX_CHARACTER`` — one process, set by a flag, never persisted."""

    SETTING = "setting"
    """``poeapi.character``. A standing choice, and the one the panel's picker
    writes. It sits **above** both derived signals; see :meth:`CharacterChoice.describe`."""

    CURRENT = "current"
    """GGG marked this character as the one being played. The truth about *now*."""

    LAST_LOGIN = "last_login"
    """Highest ``lastLoginTime``. The truth about *who played last*, which is what
    is knowable out of game and what the PoE website itself shows."""

    FALLBACK = "fallback"
    """**A guess.** Nothing said which character to read and no entry carried either
    signal, so this is merely first in the order GGG happened to send. Every surface
    is required to say so; it is the only source for which that is true."""

    NONE = "none"
    """No character at all — the roster is empty or could not be read."""


class ItemSet(Base):
    """A bag, an equipment set, or one stash tab, normalized."""

    items: list[NormalizedItem] = Field(default_factory=list)
    source: Source
    character: str | None = None

    character_source: CharacterSource | None = None
    """Why *that* character — the same discipline :attr:`league` follows, one field
    up. ``None`` on a stash tab, which has no character.

    A bag used to arrive carrying a name and nothing else, so a surface could only
    print it, and printing it was indistinguishable from having chosen it. The one
    value this exists for is :attr:`CharacterSource.FALLBACK`: it means the tool
    picked, and a header that does not say so is asserting a guess."""

    character_played_last: str | None = None
    """Who the account points at, when something stated overrode it. ``None`` when
    nothing was overridden. Lets a bag header read "PlaceholderWarden — pinned; you last
    played PlaceholderHierophant" instead of showing a pin as though it were an observation."""

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

    unsupported: str | None = None
    """Why this set is **not a complete reading of its container**, in words.

    ``None`` on every bag and on every stash tab this tool can read fully. It is set
    for a map stash tab, where the endpoint returns no items and GGG's own API models
    the tab as a parent with children that have to be fetched separately
    (:data:`modules.poeapi.backend.stash.MAP_UNSUPPORTED`).

    It is a field rather than a note on :class:`Meta` because a consumer has to be
    able to *branch* on it: an unsupported tab contributes a hole to a total, exactly
    like an ``unpriceable`` row, and must never contribute a zero. A tab that
    silently reports 0c is the failure mode this whole phase was warned about.
    """

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


class Profile(Base):
    """Who the session belongs to. The answer to ``accountName``.

    This model is what ended "no account name on record". ``get-items`` and
    ``get-stash-items`` both require an ``accountName``, no other account endpoint
    returns one, and the LAN pairing form collects a code and a credential and
    nothing else — so on a Deck there was no way to supply it, and every request
    after a successful pair failed. ``/api/profile`` answers it from the session
    cookie alone.

    Deriving it beats asking for it on more than convenience: a *wrong* account name
    produces the same 403 as an expired session, so the tool could not tell a typo
    from a dead cookie. Reading the name off the session removes that class of error
    rather than reporting it better.
    """

    account: str
    """The account name exactly as GGG spells it, discriminator included —
    ``Name#1234`` on any account renamed since the discriminators landed. It goes
    into ``accountName`` verbatim: this tool does not know GGG's naming rules, and
    trimming the suffix would be inventing one."""

    uuid: str | None = None
    """The account's stable identifier. Never sent in a request — carried because it
    is the one field that survives a rename, which is what any future "is this still
    the same account?" check would have to read."""

    meta: Meta

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


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
    """Not published by ``get-characters`` on any entry measured on 2026-08-12. Kept
    because the parser costs nothing and a field GGG restores should not need a
    model change; read it as *unknown*, not as zero."""

    current: bool = False
    """Whether GGG says this character is the one being played **right now**.

    The field is **absent from every entry** of the live roster measured on
    2026-08-12, taken out of game — so it is understood to be set only while a
    character is logged in, and that understanding is a hypothesis nobody here has
    been able to test. Nothing depends on it: :meth:`CharacterList.resolve` puts it
    first because "who is playing" beats "who played last" *if* it is ever there,
    and falls through to :attr:`last_login` when it is not, which is what every
    measurement so far has looked like.

    What it must never again mean is "the most recently played character". It was
    documented that way, it is always ``False`` out of game, and the fallback behind
    it — first in GGG's own ordering — silently read a parked Standard character on
    an account whose owner plays a league one."""

    last_login: datetime | None = None
    """When this character was last played, from GGG's ``lastLoginTime``.

    **Unix seconds**, not milliseconds: the live roster's largest value decoded to a
    timestamp one day before the day it was read, where a millisecond reading would
    have put it in January 1970. Measured, not assumed.

    This is the field the PoE website's own top bar picks its character with, and it
    is the answer to "which character am I playing" out of game — the question this
    module previously had no data for and guessed at. ``None`` means the roster
    entry carried no usable timestamp, which makes the entry ineligible to win on
    recency rather than making it lose to an invented zero. Whether a
    never-played character reports ``0`` or omits the key is unmeasured; both are
    read as ``None``."""

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class CharacterChoice(Base):
    """A resolved character and the reason it won.

    Precedence, high to low: an explicit argument, ``POEDEX_CHARACTER``, the
    ``poeapi.character`` setting, a character GGG marked ``current``, the highest
    ``lastLoginTime``, and then a visible guess.

    The two derived rungs answer different questions and are ordered accordingly.
    ``current`` says *who is playing*; ``last_login`` says *who played last*. In
    game they agree; out of game only the second exists. That ordering makes the
    panel right in the case it is actually used in — mid-session, on a Deck, with
    the game running — and still right from a desk with the game closed.
    """

    name: str | None
    source: CharacterSource
    league: str | None = None
    class_name: str | None = None
    level: int = 0
    last_login: datetime | None = None

    played_last: str | None = None
    """Who the derived signals would have picked, when something stated overrode
    them. ``None`` when nothing was overridden or when there was nothing to override.

    Present so a surface can say "reading PlaceholderWarden — pinned; you last played
    PlaceholderHierophant" instead of showing a pin as though it were an observation. A pin is
    a legitimate thing to want; a pin nobody can see is how one becomes a trap."""

    @property
    def guessed(self) -> bool:
        """``True`` when nothing in the account said which character this is.

        The one flag a surface may not render quietly. Everything else here is a
        fact about the account; this is the tool admitting it picked."""
        return self.source is CharacterSource.FALLBACK

    @property
    def stated(self) -> bool:
        """``True`` when a human said this name, rather than it being read off the
        account."""
        return self.source in (
            CharacterSource.ARGUMENT,
            CharacterSource.ENVIRONMENT,
            CharacterSource.SETTING,
        )

    @property
    def overriding(self) -> bool:
        """``True`` when a stated choice is not the character the account points at.

        Not an error and not an anomaly — reading a character other than the one you
        last played is an ordinary thing to want, which is why the picker exists.
        It is reported because a surface that shows the name alone cannot tell it
        from the tool having got it wrong."""
        return self.played_last is not None and self.played_last != self.name

    def describe(self) -> str:
        """One sentence: the name, and the claim behind it."""
        if self.name is None:
            return "no character — this account's roster is empty or unreadable"
        tail = {
            CharacterSource.ARGUMENT: "you asked for it (--character)",
            CharacterSource.ENVIRONMENT: f"{CHARACTER_ENV} is set for this run",
            CharacterSource.SETTING: "pinned by the poeapi.character setting",
            CharacterSource.CURRENT: "the API says this is the character you are playing",
            CharacterSource.LAST_LOGIN: "most recently played",
            CharacterSource.FALLBACK: (
                "GUESSED — nothing on this account says which character to read, so "
                "this is only the first one GGG listed"
            ),
        }[self.source]
        if self.source is CharacterSource.LAST_LOGIN and self.last_login is not None:
            tail = f"most recently played, {format_last_login(self.last_login)}"
        if self.overriding:
            tail += f"; you last played {self.played_last}"
        return f"{self.name} ({tail})"

    def to_json(self) -> dict[str, Any]:
        """The fields, and only the fields.

        :attr:`guessed`, :attr:`stated` and :meth:`describe` are deliberately **not**
        serialized. They are one-line functions of ``source``, the frontend has the
        same enum, and a payload carrying both the fact and four readings of it is a
        payload that can contradict itself. It is also how the generated TypeScript
        stays a projection of the model rather than of ``to_json``.
        """
        return self.model_dump(mode="json")


class CharacterList(Base):
    characters: list[Character] = Field(default_factory=list)
    meta: Meta

    def marked_current(self) -> Character | None:
        """The character GGG says is logged in, if it says so at all.

        Returns ``None`` on every roster measured so far — see :attr:`Character.current`.
        """
        for character in self.characters:
            if character.current:
                return character
        return None

    def most_recent(self) -> Character | None:
        """The character with the highest ``lastLoginTime``.

        Entries with no timestamp are skipped rather than sorted to the bottom: they
        are *unknown*, and an unknown must not be able to win by tying at zero. Ties
        between equal timestamps keep GGG's order, which is arbitrary but stable.
        """
        best: Character | None = None
        for character in self.characters:
            if character.last_login is None:
                continue
            if best is None or character.last_login > best.last_login:  # type: ignore[operator]
                best = character
        return best

    def default(self) -> Character | None:
        """Who to read when nobody has said — or ``None`` when nothing can say.

        **There is no positional fallback here, and that omission is the fix.** The
        method this replaced ended in ``self.characters[0]``, so an account where
        nothing was marked returned GGG's first entry with the same confidence as an
        observation. Callers that can degrade (a league, a realm) get ``None`` and
        say so; the one caller that must produce a name asks :meth:`resolve`, which
        labels the guess.
        """
        return self.marked_current() or self.most_recent()

    def resolve(self) -> tuple[Character | None, CharacterSource]:
        """The default character *and why*, guess included and labelled as one."""
        marked = self.marked_current()
        if marked is not None:
            return marked, CharacterSource.CURRENT
        recent = self.most_recent()
        if recent is not None:
            return recent, CharacterSource.LAST_LOGIN
        if self.characters:
            return self.characters[0], CharacterSource.FALLBACK
        return None, CharacterSource.NONE

    def named(self, name: str) -> Character | None:
        lowered = name.casefold()
        for character in self.characters:
            if character.name.casefold() == lowered:
                return character
        return None

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class CharacterSelection(Base):
    """Everything a picker needs: the roster, the pick, and the reason for it.

    One model rather than two calls, because the roster and the choice have to agree
    — a screen that fetched them separately could offer a list that does not contain
    the name it says it is reading. Costs at most one ``get-characters``, which is
    cached for an hour.
    """

    choice: CharacterChoice
    characters: list[Character] = Field(default_factory=list)
    configured: str | None = None
    """The stored ``poeapi.character`` value, or ``None`` when nothing is pinned.
    Distinct from ``choice.name``: a surface has to be able to draw the difference
    between "pinned to this" and "following the account, which currently means this"."""

    meta: Meta

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def format_last_login(when: datetime) -> str:
    """``last played 2026-08-11 14:17 UTC``. Minutes; a login is not a stopwatch."""
    return f"last played {when.astimezone(UTC):%Y-%m-%d %H:%M} UTC"


class TabKind(StrEnum):
    """The tab types this build knows about, from GGG's own ``type`` string.

    Deliberately an enum rather than the raw string: three of these decide how the
    tab is *drawn* (a quad is 24x24, a currency tab has no lattice at all) and one of
    them decides whether it can be read at all. :attr:`UNKNOWN` is a first-class
    member so a stash type GGG adds next league degrades to "listed, not placed"
    instead of being mis-drawn on an invented grid.
    """

    NORMAL = "normal"
    PREMIUM = "premium"
    QUAD = "quad"
    CURRENCY = "currency"
    ESSENCE = "essence"
    FRAGMENT = "fragment"
    DIVINATION = "divination"
    MAP = "map"
    DELVE = "delve"
    BLIGHT = "blight"
    METAMORPH = "metamorph"
    DELIRIUM = "delirium"
    ULTIMATUM = "ultimatum"
    FLASK = "flask"
    GEM = "gem"
    UNIQUE = "unique"
    FOLDER = "folder"
    UNKNOWN = "unknown"


class TabLayout(Base):
    """How to draw a tab, or an admission that it cannot be drawn as a grid.

    ``grid`` is the load-bearing field. Special tabs (currency, essence, fragment,
    divination) put items in bespoke slots and let ``stack_size`` exceed
    ``max_stack_size`` — ``Vaal Orb 163/20`` is a measured row — so one item per cell
    is simply not true of them, and a surface lists them instead.
    """

    cols: int | None = None
    rows: int | None = None
    grid: bool = False

    @property
    def cells(self) -> int | None:
        if self.cols is None or self.rows is None:
            return None
        return self.cols * self.rows


class StashTab(Base):
    index: int
    id: str | None = None
    parent: str | None = None
    """Which folder this tab is in, when it is in one. Folders exist in PoE and were
    **not** observed on the measured account, so they are modelled and not designed
    for: the list is a tree, and with no folders the tree is one level deep."""

    name: str = ""
    type: str = ""
    """GGG's raw ``type`` string, kept beside :attr:`kind` so an unrecognised tab can
    still be reported by its real name rather than as ``unknown``."""

    kind: TabKind = TabKind.UNKNOWN
    layout: TabLayout = TabLayout()
    colour: str | None = None
    hidden: bool = False
    remove_only: bool = False
    """Remove-only tabs can never gain items, so they can be fetched once and cached
    forever (research-notes §7). 86% of the measured Standard stash is remove-only."""

    children: list[StashTab] = Field(default_factory=list)
    supported: bool = True
    unsupported_reason: str | None = None
    """Why this tab's contents cannot be read. Map tabs, today — see
    :data:`modules.poeapi.backend.stash.MAP_UNSUPPORTED`."""

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class StashTabList(Base):
    league: str
    tabs: list[StashTab] = Field(default_factory=list)
    """The **roots** of the tree. Use :meth:`all_tabs` to walk it."""

    meta: Meta

    def all_tabs(self) -> list[StashTab]:
        from modules.poeapi.backend.stash import flatten

        return list(flatten(self.tabs))

    def find(self, index: int) -> StashTab | None:
        for tab in self.all_tabs():
            if tab.index == index:
                return tab
        return None

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class TabState(Base):
    """One tab's freshness, answerable **without spending a request**.

    This is what a tab list shows before anything is opened: which tabs are on disk,
    how old each copy is, and which of them can never go out of date. A surface that
    could not say that would have to either fetch everything (34 minutes) or show a
    list of names with no indication of what is real.
    """

    tab: StashTab
    cached: bool = False
    fetched_at: datetime | None = None
    age_seconds: float | None = None
    stale: bool = False
    """Older than this tab's own TTL — so never true for a remove-only tab."""

    permanent: bool = False
    """Remove-only: cached once, and correct forever."""

    item_count: int | None = None
    """How many items the cached copy holds. ``None`` when nothing is cached — which
    is *not* zero, and the distinction is the whole point of this field."""

    @property
    def needs_fetch(self) -> bool:
        return not self.cached or self.stale

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Budget(Base):
    """One learned rate-limit bucket, as the cost estimate needs it.

    Carried on :class:`StashState` rather than looked up, so that a plan can be
    computed, serialized and shown without a caller reaching back into `net`.
    """

    max_hits: int
    period: float


class StashState(Base):
    """Every tab, with its freshness, and what a full refresh would cost.

    Costs exactly one request — the tab list — and often not even that, because the
    tab list is cached for fifteen minutes.
    """

    league: str
    tabs: list[TabState] = Field(default_factory=list)
    meta: Meta
    seconds_per_request: float = 18.0
    """The **sustained** item-request rate — one per 18 s on the measured account.

    Shown, not multiplied by. It is the right number for "what does this cost me
    forever" and the wrong one for "how long is this crawl": the same policy allows
    30 requests in the first minute, so a 15-tab refresh is seconds rather than
    minutes. :attr:`buckets` is what the estimate is computed from.
    """

    buckets: list[Budget] = Field(default_factory=list)
    """Every learned item bucket. Empty until the limiter has parsed a header, in
    which case the estimate falls back to the pessimistic sustained rate — which is
    what the limiter itself will be doing."""

    @property
    def pending(self) -> list[TabState]:
        return [state for state in self.tabs if state.needs_fetch and state.tab.supported]

    @property
    def cost(self) -> CrawlPlan:
        from modules.poeapi.backend.stash import estimate_seconds

        pending = self.pending
        return CrawlPlan(
            league=self.league,
            total_tabs=len(self.tabs),
            cached_tabs=sum(1 for state in self.tabs if state.cached),
            permanent_tabs=sum(1 for state in self.tabs if state.permanent),
            unsupported_tabs=sum(1 for state in self.tabs if not state.tab.supported),
            requests=len(pending),
            seconds=estimate_seconds(
                len(pending),
                [(budget.max_hits, budget.period) for budget in self.buckets],
                self.seconds_per_request,
            ),
        )

    def to_json(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["cost"] = self.cost.to_json()
        return data


class CrawlPlan(Base):
    """What a full refresh costs right now, in requests and in wall-clock.

    SPEC §6.6: a cold crawl is user-initiated and states its cost honestly up front.
    This is that statement, and it is computed against the *real* tab list rather
    than quoted from the research note — the number falls as the cache fills, and a
    warning that keeps saying "~30 minutes" after the crawl has finished is a warning
    people learn to ignore.
    """

    league: str
    total_tabs: int = 0
    cached_tabs: int = 0
    permanent_tabs: int = 0
    unsupported_tabs: int = 0
    requests: int = 0
    seconds: float = 0.0

    @property
    def warning(self) -> str:
        if self.requests == 0:
            return "nothing to fetch: every readable tab is already cached"
        minutes = self.seconds / 60.0
        span = f"~{minutes:.0f} min" if minutes >= 1.5 else f"~{self.seconds:.0f} s"
        return (
            f"{self.requests} tab request(s), {span}, and it will pause your inventory "
            "syncing for that long — the stash and the bag share one request budget"
        )

    def to_json(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["warning"] = self.warning
        return data


class CrawlProgress(Base):
    """A crawl's disk-backed bookmark, so an interrupted one resumes.

    Written after **every** tab, not at the end. A crawl that has to start over
    because the process was killed at minute 28 is a crawl nobody runs twice.
    """

    league: str
    done: list[int] = Field(default_factory=list)
    failed: list[int] = Field(default_factory=list)
    started_at: datetime | None = None
    updated_at: datetime | None = None
    requests: int = 0
    items: int = 0

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
