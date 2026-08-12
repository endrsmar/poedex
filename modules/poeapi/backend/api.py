"""The public surface of the `poeapi` module.

**This is the only file in this module that other modules may import** (plan §1.4,
enforced by ``tests/test_boundaries.py``). It re-exports the pydantic models from
:mod:`modules.poeapi.backend.models` so that dependents get the normalized item
model of SPEC §4.5 without reaching past this file — the models *are* the public
surface, and they are the single source of truth for the generated TypeScript types.

Every accessor here returns a model carrying its own freshness (``meta``). That is
what lets a surface distinguish *fresh*, *stale because we refused to spend budget*,
and *stale because the session is dead* instead of showing three different failures
as one blank panel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from modules.poeapi.backend.models import (
    CHARACTER_ENV,
    Budget,
    Character,
    CharacterChoice,
    CharacterList,
    CharacterSelection,
    CharacterSource,
    CrawlPlan,
    CrawlProgress,
    Grid,
    ItemSet,
    Location,
    Meta,
    Mods,
    NormalizedItem,
    Profile,
    Rarity,
    Sockets,
    Source,
    StashState,
    StashTab,
    StashTabList,
    TabKind,
    TabLayout,
    TabState,
    format_last_login,
)
from modules.poeapi.backend.stash import (
    FOREVER,
    MAP_UNSUPPORTED,
    QUAD_COLUMNS,
    STANDARD_COLUMNS,
    layout_for,
    tab_kind,
    unsupported_reason,
)
from runtime.errors import PoedexError

# ``CHARACTER_ENV`` is defined in `models.py` and re-exported here, which is where
# every dependent still imports it from. A surface may set it; the resolver consults
# it after an explicit argument and before the persisted setting, for the same reason
# ``POEDEX_GAMELOG_PATH`` exists: ``serve --character`` must not rewrite the user's
# settings file just because they wanted to look at a different character once.

__all__ = [
    "CHARACTERS_PATH",
    "CHARACTER_CHANGED",
    "CHARACTER_ENV",
    "FOREVER",
    "ITEMS_PATH",
    "MAP_UNSUPPORTED",
    "PROFILE_PATH",
    "QUAD_COLUMNS",
    "STANDARD_COLUMNS",
    "STASH_PATH",
    "SYNC_COMPLETE",
    "AccountUnknownError",
    "Budget",
    "Character",
    "CharacterChoice",
    "CharacterList",
    "CharacterSelection",
    "CharacterSource",
    "CharacterUnknownError",
    "CrawlPlan",
    "CrawlProgress",
    "CrawlStep",
    "Grid",
    "ItemSet",
    "LeagueUnknownError",
    "Location",
    "Meta",
    "Mods",
    "NormalizedItem",
    "PoeApi",
    "PoeApiError",
    "Profile",
    "Rarity",
    "SessionRejectedError",
    "Sockets",
    "Source",
    "StashState",
    "StashTab",
    "StashTabList",
    "TabKind",
    "TabLayout",
    "TabState",
    "format_last_login",
    "layout_for",
    "tab_kind",
    "unsupported_reason",
]

SYNC_COMPLETE = "sync_complete"
"""Event topic emitted after a successful live fetch. Payload carries the source,
the content hash and whether anything changed."""

CHARACTER_CHANGED = "character_changed"
"""Event topic emitted when ``poeapi.character`` is pinned or cleared.

Payload is a :meth:`CharacterSelection.to_json`. It exists because the pick is made
on one screen and spent on another: a player who pins a character in the panel's
picker and presses **B** must not find the bag screen behind it still showing the
other character's items, and a surface cannot know to re-read what a different
screen wrote.
"""

# SPEC §4.2. Kept here rather than in the implementation because the rate limiter
# buckets by route and a caller may legitimately want to ask `net.retry_after` about
# one of these before deciding whether to offer a refresh button.
CHARACTERS_PATH = "/character-window/get-characters"
ITEMS_PATH = "/character-window/get-items"
STASH_PATH = "/character-window/get-stash-items"

PROFILE_PATH = "/api/profile"
"""Who the session belongs to, from the cookie alone.

Not in SPEC §4.2's original table, because Phase 2 recorded that the account name
was not inferable and asked for it instead. It is inferable: this endpoint answers
``{"uuid": ..., "name": "Name#1234", ...}`` for whoever the POESESSID belongs to,
with no ``accountName`` of its own to supply — which is what makes it the one
account endpoint that can bootstrap the others.
"""


class PoeApiError(PoedexError):
    """A problem fetching or interpreting account data."""


class SessionRejectedError(PoeApiError):
    """The API rejected the session (401/403).

    Raised *after* `credentials` has been told, so a surface can catch this and show
    "your session expired, pair again" knowing the stored state already agrees.
    """


class AccountUnknownError(PoeApiError):
    """``get-items`` needs an account name and nothing could produce one.

    Rare, and narrower than it was. Four sources are tried in order — an explicit
    argument, the ``poeapi.account`` setting, the name filed with the credential, and
    :meth:`PoeApi.get_profile` — so this is raised only when the last of those could
    not be reached *and* was not an auth failure (which raises
    :class:`SessionRejectedError` instead, because "your session is dead" is the
    truer sentence). In practice: no credential at all, the profile endpoint rate
    limited or down with nothing cached, or a profile response with no name in it.

    The message says which of those happened. It used to say "run ``poedex auth set
    --account <name>``", which was an instruction to type an account name on a device
    with no keyboard, for a value the tool can now read for itself.
    """


class CharacterUnknownError(PoeApiError):
    """A character was named that this account's roster does not contain.

    Raised only by :meth:`PoeApi.set_character`, and it is the rule that keeps a
    picker honest: the panel may write ``poeapi.character``, and the one thing it
    may not write is a name nobody can play. The message lists the names that do
    exist, because on a Deck the alternative to reading them is typing.

    Note that :meth:`PoeApi.get_items` deliberately does **not** raise this. A name
    passed explicitly is sent to GGG as given — a roster fetched an hour ago is not
    the authority on whether a character exists, and refusing a request because a
    cache disagrees would be a worse failure than the 403 the API would answer with.
    """


class LeagueUnknownError(PoeApiError):
    """Which league to act on could not be established, and guessing is not allowed.

    Defined here, in the core module that owns the concept, and re-exported by
    ``prices`` so both raise **one** type. A league is the axis every price is
    denominated on: the same Divine Orb was 897.7c in Standard and 209.0c in
    Allflame on the day this project measured them, so a default league is not a
    convenience, it is a four-fold error with a confident face on it. Refusing is
    the only honest answer when nothing knows the league.
    """


class RateLimitedError(PoeApiError):
    """A live fetch was refused and there was no cached copy to fall back on."""

    def __init__(self, retry_after: float, reason: str = "") -> None:
        self.retry_after = max(0.0, float(retry_after))
        self.reason = reason
        detail = f" ({reason})" if reason else ""
        super().__init__(f"rate limited: retry in {self.retry_after:.0f}s{detail}")


@dataclass(frozen=True, slots=True)
class CrawlStep:
    """One tab of a crawl, reported the moment it lands.

    A crawl is minutes long, so it reports per tab rather than at the end: a caller
    can draw progress, write a digest incrementally, and — because
    :attr:`CrawlProgress` is written after each of these — resume from here.
    """

    tab: StashTab
    index: int
    """Position within this crawl, 1-based. Not the tab index."""

    total: int
    items: ItemSet | None = None
    error: str | None = None
    from_cache: bool = False
    """``True`` when the tab was already on disk and no request was spent. Every
    remove-only tab is this on the second crawl, which is the 34-minutes-to-45-seconds
    rule showing up as a number a caller can print."""

    @property
    def spent(self) -> int:
        return 0 if self.from_cache or self.items is None else 1


@runtime_checkable
class PoeApi(Protocol):
    """What dependents get from ``ctx.require(PoeApi)``."""

    async def get_characters(
        self, *, refresh: bool = False, realm: str | None = None
    ) -> CharacterList:
        """The account's characters. Cached hard; never poll this (SPEC §4.4).

        ``realm`` is the one request that cannot read the realm off the roster,
        because this is the roster. Left unset, the parameter is omitted and GGG
        answers for its own default; every later request reads the realm out of
        the entries this returns.
        """
        ...

    async def character_choice(
        self, character: str | None = None, *, refresh: bool = False
    ) -> CharacterSelection:
        """Which character would be read, why, and every character it could be.

        The accessor a picker opens on and the one ``poedex characters`` prints.
        Costs at most one ``get-characters``, which is cached for an hour, so asking
        "who am I about to read?" is affordable enough that a surface can ask before
        every screen instead of showing a name with no provenance.

        ``choice.source`` is the reason, and :attr:`CharacterChoice.guessed` is the
        one value a surface may not render quietly.
        """
        ...

    async def set_character(self, name: str | None) -> CharacterSelection:
        """Pin ``poeapi.character``, or clear it with ``None`` / ``""``.

        The write behind the panel's picker, and the only way to change which
        character is read on a Deck: the plugin reads ``DECKY_PLUGIN_SETTINGS_DIR``
        rather than ``~/.config/poedex``, and there is no usable CLI inside the
        plugin tree to run ``poedex config set`` with.

        The name is checked against the roster and
        :class:`CharacterUnknownError` is raised if it is not there — a picker may
        pin, and may not invent. Returns the new selection so a caller re-renders
        from the backend's answer rather than from what it hoped it wrote.
        """
        ...

    async def get_profile(self, *, refresh: bool = False) -> Profile:
        """Which account this session belongs to. Cached for a day; never poll it.

        The only account endpoint that needs no ``accountName``, which is what makes
        it the one that can supply it to the others. Every caller that needs a name
        gets it through :meth:`get_items` and friends resolving one — this is on the
        Protocol so a surface can *show* the account, and so a pairing flow can file
        the name with the credential the moment it lands.

        A 401/403 here is treated exactly as it is on every other endpoint:
        `credentials` is told first, then :class:`SessionRejectedError` is raised.
        """
        ...

    async def get_items(
        self,
        character: str | None = None,
        *,
        account: str | None = None,
        refresh: bool = False,
        realm: str | None = None,
    ) -> ItemSet:
        """Backpack and worn gear for one character, normalized.

        ``character`` defaults to whatever :meth:`character_choice` resolves — the
        pinned one, else the one GGG marks as being played, else the highest
        ``lastLoginTime``. Items are tagged
        :attr:`Source.BAG` or :attr:`Source.EQUIPMENT`; the bag is what appraisal
        cares about.

        The returned :attr:`ItemSet.league` is **the league that character is in**,
        read off the cached character list rather than off a setting. It is the
        only place that fact is knowable, so dropping it here is what made every
        downstream consumer fall back to a default and price the wrong economy.
        ``None`` means it could not be established — never "Standard".

        ``realm`` follows the same discipline one rung down: the argument, then the
        ``poeapi.realm`` setting, then the roster entry this character came from.
        """
        ...

    async def get_stash_tabs(
        self, league: str | None = None, *, refresh: bool = False, realm: str | None = None
    ) -> StashTabList:
        """The tab list for a league. Shares a rate-limit bucket with ``get_items``.

        ``league`` defaults to the ``poeapi.league`` setting and then to the current
        character's league; when neither is available this raises
        :class:`LeagueUnknownError` rather than reading somebody else's stash.
        """
        ...

    async def get_stash_items(
        self,
        tab_index: int,
        league: str | None = None,
        *,
        refresh: bool = False,
        realm: str | None = None,
    ) -> ItemSet:
        """One stash tab's contents, normalized. **One request per tab; there is no
        batch endpoint**, and this endpoint shares ``backend-item-request-limit``
        with ``get_items`` — one item request per 18 seconds, sustained.

        Two rules ride on the tab's own metadata, both from research-notes §7:

        * A **remove-only** tab is cached forever. It cannot gain items, so a second
          fetch spends budget to learn nothing. ``refresh=True`` still overrides.
        * A **map** tab comes back with ``unsupported`` set and no items, because the
          endpoint returns none and the zero cannot be trusted (:data:`MAP_UNSUPPORTED`).
        """
        ...

    async def cached_stash_items(
        self, tab_index: int, league: str | None = None
    ) -> ItemSet | None:
        """A tab's contents if they are already on disk, ``None`` if they are not.

        The accessor a digest is built from: it can walk 117 tabs and spend nothing.
        ``None`` means *not read yet*, which a surface must be able to distinguish
        from *empty* — the two look identical in a total and mean opposite things.
        """
        ...

    async def stash_state(
        self, league: str | None = None, *, refresh: bool = False
    ) -> StashState:
        """Every tab with its freshness, and what a full refresh would cost.

        Costs at most one request — the tab list — and **never** fetches a tab. This
        is the accessor a stash screen opens on: it answers "what is on disk, how old
        is it, and what would the rest cost" without touching the item budget.
        """
        ...

    def crawl_stash(
        self,
        league: str | None = None,
        *,
        resume: bool = True,
        limit: int | None = None,
        refresh: bool = False,
    ) -> Any:
        """A cold crawl, as an async iterator of :class:`CrawlStep`.

        **Never called automatically.** SPEC §6.6: a crawl is user-initiated,
        resumable, disk-backed, and states its cost up front — :meth:`stash_state`'s
        ``cost`` is that statement. Nothing in this module schedules one, and nothing
        in a surface may start one without a press.

        Resumable because it is minutes long: progress is written to disk after every
        tab, and ``resume=True`` skips what an earlier run finished.
        """
        ...

    async def crawl_progress(self, league: str | None = None) -> CrawlProgress | None:
        """The bookmark an interrupted crawl left behind, if any. Spends nothing."""
        ...

    def limits(self) -> list[dict[str, Any]]:
        """The rate limiter's current view, for an honest "why not yet" message."""
        ...
