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

from typing import Any, Protocol, runtime_checkable

from modules.poeapi.backend.models import (
    Character,
    CharacterList,
    Grid,
    ItemSet,
    Location,
    Meta,
    Mods,
    NormalizedItem,
    Rarity,
    Sockets,
    Source,
    StashTab,
    StashTabList,
)
from runtime.errors import PoedexError

CHARACTER_ENV = "POEDEX_CHARACTER"
"""Which character to read, for the lifetime of one process.

A surface may set this; :meth:`_character` consults it after an explicit argument
and before the persisted setting. It exists for the same reason
``POEDEX_GAMELOG_PATH`` does: ``serve --character`` must not rewrite the user's
settings file just because they wanted to look at a different character once.
"""

__all__ = [
    "CHARACTERS_PATH",
    "CHARACTER_ENV",
    "ITEMS_PATH",
    "STASH_PATH",
    "SYNC_COMPLETE",
    "AccountUnknownError",
    "Character",
    "CharacterList",
    "Grid",
    "ItemSet",
    "LeagueUnknownError",
    "Location",
    "Meta",
    "Mods",
    "NormalizedItem",
    "PoeApi",
    "PoeApiError",
    "Rarity",
    "SessionRejectedError",
    "Sockets",
    "Source",
    "StashTab",
    "StashTabList",
]

SYNC_COMPLETE = "sync_complete"
"""Event topic emitted after a successful live fetch. Payload carries the source,
the content hash and whether anything changed."""

# SPEC §4.2. Kept here rather than in the implementation because the rate limiter
# buckets by route and a caller may legitimately want to ask `net.retry_after` about
# one of these before deciding whether to offer a refresh button.
CHARACTERS_PATH = "/character-window/get-characters"
ITEMS_PATH = "/character-window/get-items"
STASH_PATH = "/character-window/get-stash-items"


class PoeApiError(PoedexError):
    """A problem fetching or interpreting account data."""


class SessionRejectedError(PoeApiError):
    """The API rejected the session (401/403).

    Raised *after* `credentials` has been told, so a surface can catch this and show
    "your session expired, pair again" knowing the stored state already agrees.
    """


class AccountUnknownError(PoeApiError):
    """``get-items`` needs an account name and none is on record.

    Not inferable: the character endpoint does not return it, and guessing produces
    a 403 that looks exactly like an expired session.
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


@runtime_checkable
class PoeApi(Protocol):
    """What dependents get from ``ctx.require(PoeApi)``."""

    async def get_characters(self, *, refresh: bool = False) -> CharacterList:
        """The account's characters. Cached hard; never poll this (SPEC §4.4)."""
        ...

    async def get_items(
        self,
        character: str | None = None,
        *,
        account: str | None = None,
        refresh: bool = False,
    ) -> ItemSet:
        """Backpack and worn gear for one character, normalized.

        ``character`` defaults to the most recently played one, which is what the
        character endpoint marks as current. Items are tagged
        :attr:`Source.BAG` or :attr:`Source.EQUIPMENT`; the bag is what appraisal
        cares about.

        The returned :attr:`ItemSet.league` is **the league that character is in**,
        read off the cached character list rather than off a setting. It is the
        only place that fact is knowable, so dropping it here is what made every
        downstream consumer fall back to a default and price the wrong economy.
        ``None`` means it could not be established — never "Standard".
        """
        ...

    async def get_stash_tabs(
        self, league: str | None = None, *, refresh: bool = False
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
    ) -> ItemSet:
        """One stash tab's contents, normalized."""
        ...

    def limits(self) -> list[dict[str, Any]]:
        """The rate limiter's current view, for an honest "why not yet" message."""
        ...
