"""The `poeapi` core module: endpoints, normalization, cache.

Core because it holds no feature opinion. It fetches what SPEC §4.2 lists, turns it
into the model of SPEC §4.5, and reports honestly how fresh the answer is. What to
*do* with a stale bag is a feature decision.

Three behaviours here exist to protect the account rather than to serve a caller:

* **``get-characters`` has a hard minimum interval** that ``refresh=True`` cannot
  override. It is the tightest endpoint on the account (``10:60``, ``50:1800``) and
  the data — a character's name and league — changes about once a league.
* **A refused fetch degrades to cached data**, flagged ``stale`` with the retry
  time, instead of either raising or quietly queueing. The UI can then say "as of
  four minutes ago, refresh available in 12 s", which is true.
* **A 401/403 tells `credentials` before it reaches the caller**, so the stored
  state and the error the surface sees can never disagree.

One that exists so the tool works at all on a Deck: **the account name is derived
from the session, not asked for**. ``get-items`` and ``get-stash-items`` both require
an ``accountName``; the LAN pairing form collects a code and a credential and there
is no third field, so a Deck used to pair successfully and then fail every request
with "no account name on record". ``/api/profile`` answers it from the cookie alone,
and :meth:`PoeApiModule._account` consults it after the two stated sources and files
the answer with the credential. It is also strictly *more correct* than asking: a
wrong account name comes back as the same 403 as an expired session, and there is
now nothing to get wrong.

And one that exists to protect the *numbers*: **every :class:`ItemSet` carries the
league it came from**, read off the character list rather than off a setting. This
module is the only place in the tool where "which league is this?" has a truthful
answer, and it used to throw it away — leaving `prices` to fall back to a default
and denominate an Allflame bag in Standard chaos, a factor of four on the divine
rate. Resolving it costs no extra request: ``get-characters`` is cached for an hour
and the default-character lookup has usually just made the call. The same roster
entry also answers **which realm the account is on**, and for the same reason —
see :data:`NO_REALM`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import ValidationError

from modules.credentials.backend.api import CredentialsApi, CredentialState
from modules.net.backend.api import AuthRejected, HttpStatusError, NetApi, NetError, RateLimited
from modules.poeapi.backend.api import (
    CHARACTER_ENV,
    CHARACTERS_PATH,
    ITEMS_PATH,
    PROFILE_PATH,
    STASH_PATH,
    SYNC_COMPLETE,
    AccountUnknownError,
    Budget,
    Character,
    CharacterList,
    CrawlProgress,
    CrawlStep,
    ItemSet,
    LeagueUnknownError,
    Meta,
    PoeApi,
    PoeApiError,
    Profile,
    RateLimitedError,
    SessionRejectedError,
    Source,
    StashState,
    StashTab,
    StashTabList,
    TabKind,
    TabState,
)
from modules.poeapi.backend.cache import CacheEntry, ResponseCache
from modules.poeapi.backend.models import utcnow
from modules.poeapi.backend.normalize import normalize_items, strip_set_tokens
from modules.poeapi.backend.stash import (
    crawl_key,
    flatten,
    tab_kind,
    tab_ttl,
    tabs_from,
    unsupported_reason,
)
from runtime.context import ModuleContext
from runtime.errors import ModuleNotStartedError
from runtime.log import get_logger
from runtime.storage import Storage

__all__ = ["MODULE", "PoeApiModule"]

_fallback_log = get_logger("module.poeapi")

# `get-items` and `get-stash-items` share `backend-item-request-limit`, so they share
# buckets automatically once the policy is learned. The route names below only decide
# which *seed* budget applies before the first response teaches us anything, which is
# why the two item endpoints share one and characters gets its own.
ITEM_ROUTE = "character-window:items"
CHARACTER_ROUTE = "character-window:characters"

PROFILE_ROUTE = "profile"
"""Its own route, not shared with the character endpoint.

Route names only decide the *seed* budget — one request per ten seconds — that
applies before a response header teaches the limiter the real policy; after that,
two paths GGG governs with one policy converge on one bucket by themselves. Sharing
the character seed would mean a profile read could hold up a roster read for ten
seconds on a cold start, which is precisely the first thing a freshly paired Deck
does.
"""

# `refresh=True` cannot go below this on the character endpoint. SPEC §4.4: cache
# hard, never poll.
CHARACTERS_MIN_INTERVAL = 60.0

PROFILE_TTL = 86400.0
"""A day, and not a setting.

An account name changes when a player pays GGG to change it. There is no tuning
question here, so there is no knob: a setting would be one more thing on a settings
screen that nobody can answer better than this constant does.

There is deliberately **no** ``min_interval`` to go with it. The one caller that
passes ``refresh=True`` is pairing, which happens because a human walked to another
computer, and it is the case where the cached answer is most likely to be the
*previous* account's. The rate limiter is what protects the endpoint; this only
decides how long an answer stays good.
"""

DEFAULT_CHARACTERS_TTL = 3600
DEFAULT_ITEMS_TTL = 0
DEFAULT_STASH_TABS_TTL = 900
DEFAULT_STASH_ITEMS_TTL = 20

NO_LEAGUE = ""
"""The ``league`` setting's default, and it is deliberately empty.

It used to be ``"Standard"``. That is the single most expensive default this
project has had: it is *plausible* — most accounts have a Standard character — so
nothing ever looks broken, and it silently answers a question ("which economy?")
that only the character list can answer. Empty means "ask the account", and the
code paths below either find the answer or raise :class:`LeagueUnknownError`.
"""

NO_REALM = ""
"""The ``realm`` setting's default, and empty for the same reason as ``league``.

This module used to carry ``REALM = "pc"`` and put it in the query string of every
character-window request. That is the league bug again at lower stakes: a constant
standing in for an account fact the API already returns. ``get-characters`` reports
a ``realm`` per character, and it is read off the same roster entry the league is.

Empty means "ask the account". When nothing can answer — no setting, and no roster
because the endpoint refused — the parameter is **left off the request** rather than
guessed, and a warning says so. Leaving it off is not the same as knowing: GGG will
answer for whatever realm it defaults to, and on a console account that is the wrong
one. There is no ``choices`` list on the setting either: ``pc``, ``xbox`` and
``sony`` are the three this build knows of, and refusing anything else would be the
same mistake from the other side — this module deciding what GGG's realms are.

**Unverified.** Nobody involved has a console account, so the console path is
reasoned, not measured — and whether GGG's legacy endpoints require the parameter at
all, rather than defaulting when it is absent, is likewise unmeasured. If a request
without it turns out to be refused, the fix is to set the realm explicitly:
``poedex config set poeapi.realm pc``.
"""

MEASURED_ITEM_INTERVAL = 18.0
"""Seconds per item request, sustained — measured (research-notes §3, CLAUDE.md).

The **fallback** for a cost estimate before the limiter has parsed a real header, and
never used for pacing: `net` paces from ``X-Rate-Limit-*`` and this module has no
business having a second opinion. It is here so that "how long would a crawl take?"
has an answer on a cold start, and :meth:`PoeApiModule.seconds_per_item_request`
replaces it with the learned figure the moment there is one.
"""

# How stale the credential's "last confirmed" timestamp may get before a successful
# fetch refreshes it. Every refresh is a disk write plus a `credential_changed`
# event, and the fact being recorded barely changes.
MARK_OK_INTERVAL = 300.0


class PoeApiModule:
    id = "poeapi"
    name = "Path of Exile API"
    kind = "core"
    requires: ClassVar[list[str]] = ["credentials", "net"]
    provides: type | None = PoeApi

    def __init__(self, *, cache: ResponseCache | None = None) -> None:
        self._cache = cache
        self._ctx: ModuleContext | None = None
        self._net: NetApi | None = None
        self._credentials: CredentialsApi | None = None
        self._hashes: dict[str, str] = {}
        self._storage: Storage | None = None
        self._requests_made = 0
        """Live fetches this process has made. A crawl reads it to tell "this tab cost
        a request" from "this tab was already on disk", which is the difference
        between a 34-minute refresh and a 45-second one."""

    # -- lifecycle -------------------------------------------------------------

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        self._net = ctx.require(NetApi)
        self._credentials = ctx.require(CredentialsApi)
        self._storage = ctx.storage
        if self._cache is None:
            self._cache = ResponseCache(ctx.storage)
        # `credentials` cannot ask GGG anything — nothing on that side of the
        # boundary knows how — so it is handed something that can. This is what lets
        # a LAN pair file the account name with the credential the moment it lands,
        # on a device where the alternative was typing it.
        self._credentials.set_account_resolver(self._resolve_account)
        configured = str(self._setting("league", NO_LEAGUE)).strip()
        ctx.logger.info(
            "poeapi ready: league=%s",
            configured or "unset (read from the character being synced)",
        )

    async def stop(self) -> None:
        if self._credentials is not None:
            # Before the references go: a resolver left behind would be a closure
            # over a stopped module, and pairing would call into `_require_net` and
            # get `ModuleNotStartedError` instead of a name.
            self._credentials.set_account_resolver(None)
        self._ctx = None
        self._net = None
        self._credentials = None
        self._storage = None

    def methods(self) -> dict[str, Callable[..., Any]]:
        """No ``crawl_stash`` here, deliberately.

        A crawl is minutes long and spends the account's whole item budget; putting it
        behind a method call would let any surface start one with a single dispatch,
        which is precisely what SPEC §6.6's "never auto-crawl" forbids. ``stash_state``
        is registered instead — it costs at most the tab list and it *reports* what a
        crawl would cost, which is the thing a surface actually needs.
        """
        return {
            "get_profile": self.get_profile_json,
            "get_characters": self.get_characters_json,
            "get_items": self.get_items_json,
            "get_stash_tabs": self.get_stash_tabs_json,
            "get_stash_items": self.get_stash_items_json,
            "stash_state": self.stash_state_json,
            "limits": self.limits_json,
        }

    def settings_schema(self) -> dict[str, Any]:
        return {
            "league": {
                "type": "str",
                "default": NO_LEAGUE,
                "label": "League",
                "description": (
                    "Which league's stash to read. Leave empty to follow the "
                    "character you are playing; set it only to read a different "
                    "league's stash. A bag never uses this — it carries the league "
                    "of the character it came from."
                ),
            },
            "realm": {
                "type": "str",
                "default": NO_REALM,
                "label": "Realm",
                "description": (
                    "Which realm the account is on — 'pc', 'xbox' or 'sony'. Leave "
                    "empty to read it off the character list, which is where it "
                    "comes from; set it only if that list cannot be reached or "
                    "names a realm this build does not know about."
                ),
            },
            "account": {
                "type": "str",
                "default": "",
                "label": "Account name",
                "description": (
                    "Whose items to read. Leave empty and it is read off your "
                    "session, which is what a Steam Deck with no keyboard depends "
                    "on; fill it in only to override a derived name that is wrong."
                ),
            },
            "character": {
                "type": "str",
                "default": "",
                "label": "Character",
                "description": (
                    "Which character to read. Leave empty to follow whoever you "
                    "played most recently, which is usually what you want. Set it "
                    "to pin a surface that has no way to ask — the web page reads "
                    "this, since a browser tab cannot pass a flag."
                ),
            },
            "characters_ttl_seconds": {
                "type": "int",
                "default": DEFAULT_CHARACTERS_TTL,
                "min": int(CHARACTERS_MIN_INTERVAL),
                "max": 86400,
                "label": "Character list cache",
                "description": (
                    "get-characters is the tightest endpoint on the account "
                    "(10:60, 50:1800). Lowering this buys nothing: character names "
                    "change about once a league."
                ),
            },
            "items_ttl_seconds": {
                "type": "int",
                "default": DEFAULT_ITEMS_TTL,
                "min": 0,
                "max": 3600,
                "label": "Inventory cache",
                "description": (
                    "0 means every call fetches. The endpoint commits at zone "
                    "transitions, so syncing is event-driven and a TTL here would "
                    "only delay the one sync that matters."
                ),
            },
            "stash_tabs_ttl_seconds": {
                "type": "int",
                "default": DEFAULT_STASH_TABS_TTL,
                "min": 0,
                "max": 86400,
                "label": "Stash tab list cache",
            },
            "stash_items_ttl_seconds": {
                "type": "int",
                "default": DEFAULT_STASH_ITEMS_TTL,
                "min": 0,
                "max": 3600,
                "label": "Stash tab cache",
            },
        }

    # -- PoeApi ----------------------------------------------------------------

    async def get_profile(self, *, refresh: bool = False) -> Profile:
        """Who this session belongs to. One request, then a day of cache.

        The endpoint takes no ``accountName`` — it answers for whoever holds the
        cookie — which is the whole reason it can bootstrap the two endpoints that
        do. It goes through :meth:`_fetch` like everything else, so it shares the
        cache discipline, the degrade-to-cache behaviour, and the 401/403 path that
        tells `credentials` before the caller hears about it.
        """
        payload, meta = await self._fetch(
            path=PROFILE_PATH,
            route=PROFILE_ROUTE,
            params={},
            cache_key="profile",
            ttl=PROFILE_TTL,
            refresh=refresh,
        )
        name = _profile_name(payload)
        if not name:
            raise PoeApiError("the profile endpoint answered without an account name")
        return Profile(account=name, uuid=_profile_uuid(payload), meta=meta)

    async def get_characters(
        self, *, refresh: bool = False, realm: str | None = None
    ) -> CharacterList:
        cache_key = "characters"
        ttl = float(self._setting("characters_ttl_seconds", DEFAULT_CHARACTERS_TTL))
        # The one request that cannot consult the roster for its realm: this *is*
        # the roster. Argument or setting only, and otherwise no realm parameter —
        # the response is what answers the question for everything after it.
        payload, meta = await self._fetch(
            path=CHARACTERS_PATH,
            route=CHARACTER_ROUTE,
            params=_realm_param({}, self._configured_realm(realm)),
            cache_key=cache_key,
            ttl=ttl,
            refresh=refresh,
            min_interval=CHARACTERS_MIN_INTERVAL,
        )
        return CharacterList(characters=_characters_from(payload), meta=meta)

    async def get_items(
        self,
        character: str | None = None,
        *,
        account: str | None = None,
        refresh: bool = False,
        realm: str | None = None,
    ) -> ItemSet:
        name, roster = await self._character(character)
        # One roster answers both questions. Resolved here rather than inside each
        # helper so a named character costs the same single cached lookup that the
        # default-character path has already paid for.
        if roster is None:
            roster = await self._roster()
        league_name = await self._character_league(name, roster)
        realm_name = await self._realm(realm, character=name, roster=roster, may_fetch=False)
        account_name = await self._account(account)
        payload, meta = await self._fetch(
            path=ITEMS_PATH,
            route=ITEM_ROUTE,
            params=_realm_param(
                {"accountName": account_name, "character": name}, realm_name
            ),
            cache_key=f"items:{account_name}:{name}",
            ttl=float(self._setting("items_ttl_seconds", DEFAULT_ITEMS_TTL)),
            refresh=refresh,
        )
        raw_items = payload.get("items") if isinstance(payload, Mapping) else None
        items = normalize_items(raw_items or [], source=Source.BAG, split_equipment=True)
        result = ItemSet(
            items=items,
            source=Source.BAG,
            character=name,
            league=league_name,
            meta=meta,
        )
        await self._announce(f"items:{name}", result)
        return result

    async def get_stash_tabs(
        self, league: str | None = None, *, refresh: bool = False, realm: str | None = None
    ) -> StashTabList:
        league_name = await self._league(league)
        realm_name = await self._realm(realm)
        account_name = await self._account(None)
        payload, meta = await self._fetch(
            path=STASH_PATH,
            route=ITEM_ROUTE,
            params=_realm_param(
                {
                    "accountName": account_name,
                    "league": league_name,
                    "tabs": 1,
                    "tabIndex": 0,
                },
                realm_name,
            ),
            cache_key=self._tabs_key(account_name, league_name),
            ttl=float(self._setting("stash_tabs_ttl_seconds", DEFAULT_STASH_TABS_TTL)),
            refresh=refresh,
        )
        return StashTabList(league=league_name, tabs=tabs_from(payload), meta=meta)

    async def get_stash_items(
        self,
        tab_index: int,
        league: str | None = None,
        *,
        refresh: bool = False,
        realm: str | None = None,
    ) -> ItemSet:
        """One tab, one request — with the two rules research-notes §7 measured.

        The tab's own metadata decides both, and it is read out of the **cached** tab
        list rather than fetched: a lazy open is meant to cost one request, and
        checking whether it is allowed to cost one must not cost another.
        """
        league_name = await self._league(league)
        realm_name = await self._realm(realm)
        account_name = await self._account(None)
        known = self._known_tab(account_name, league_name, tab_index)

        if known is not None and not known.supported:
            # A map tab, refused before the request rather than after. Spending an
            # item request to be told nothing — on 10 of the 117 measured tabs — is
            # budget spent to produce a zero that would be wrong.
            return ItemSet(
                items=[],
                source=Source.STASH,
                league=league_name,
                tab_index=tab_index,
                tab_name=known.name or None,
                unsupported=known.unsupported_reason,
                meta=Meta(fetched_at=utcnow(), note=known.unsupported_reason),
            )

        default_ttl = float(self._setting("stash_items_ttl_seconds", DEFAULT_STASH_ITEMS_TTL))
        payload, meta = await self._fetch(
            path=STASH_PATH,
            route=ITEM_ROUTE,
            params=_realm_param(
                {
                    "accountName": account_name,
                    "league": league_name,
                    # `tabs=1` costs nothing extra — it is the same request — and it
                    # is what makes a *cold* read of one tab still able to tell a map
                    # tab from a premium one. Without the tab list riding along, an
                    # unknown tab type would let a map tab's empty answer through as a
                    # confident zero, which is the failure this phase exists to avoid.
                    "tabs": 1,
                    "tabIndex": tab_index,
                },
                realm_name,
            ),
            cache_key=self._items_key(account_name, league_name, tab_index),
            # Remove-only tabs can never gain items, so their copy never expires
            # (research-notes §7). `refresh=True` still overrides, because a tab can
            # still *lose* items and that goes through the player.
            ttl=tab_ttl(known, default_ttl),
            refresh=refresh,
        )
        if not meta.from_cache:
            # The tab list rode along with the items, so file it. Without this, the
            # very first tab a player opens costs two requests — one for the items and
            # one for a list that was already in the response — and "open a tab, one
            # request" (SPEC §6.6) would be true only from the second tab onwards.
            self._remember_tabs(account_name, league_name, payload)
        raw_items = payload.get("items") if isinstance(payload, Mapping) else None
        tab_name = _tab_name(payload, tab_index) or (known.name if known else None)
        items = normalize_items(
            raw_items or [],
            source=Source.STASH,
            tab_index=tab_index,
            tab_name=tab_name,
        )
        kind = known.kind if known is not None else _kind_in(payload, tab_index)
        # Asked again *with the count*: a map tab that came back with items is not
        # reported unsupported. It is the zero that is untrustworthy, not the tab.
        blocked = unsupported_reason(kind, item_count=len(items))
        result = ItemSet(
            items=items,
            source=Source.STASH,
            league=league_name,
            tab_index=tab_index,
            tab_name=tab_name,
            unsupported=blocked,
            meta=meta if blocked is None else meta.model_copy(update={"note": blocked}),
        )
        await self._announce(f"stash:{league_name}:{tab_index}", result)
        return result

    async def cached_stash_items(self, tab_index: int, league: str | None = None) -> ItemSet | None:
        """A tab's contents **if they are already on disk**, and ``None`` otherwise.

        Exists so that a digest over 117 tabs can be built without a single request.
        :meth:`get_stash_items` would be wrong for that: a tab whose 20-second TTL has
        passed is a *fetch*, and a screen that quietly turned into 117 of them is
        precisely the auto-crawl SPEC §6.6 forbids. ``None`` here means "not read
        yet", which is a thing a surface must be able to say — and is not zero.
        """
        league_name = await self._league(league)
        account_name = await self._account(None)
        entry = self._require_cache().get(self._items_key(account_name, league_name, tab_index))
        known = self._known_tab(account_name, league_name, tab_index)
        if known is not None and not known.supported:
            return ItemSet(
                items=[],
                source=Source.STASH,
                league=league_name,
                tab_index=tab_index,
                tab_name=known.name or None,
                unsupported=known.unsupported_reason,
                meta=Meta(fetched_at=utcnow(), note=known.unsupported_reason),
            )
        if entry is None:
            return None
        payload = entry.payload
        raw_items = payload.get("items") if isinstance(payload, Mapping) else None
        tab_name = _tab_name(payload, tab_index) or (known.name if known else None)
        items = normalize_items(
            raw_items or [], source=Source.STASH, tab_index=tab_index, tab_name=tab_name
        )
        kind = known.kind if known is not None else _kind_in(payload, tab_index)
        blocked = unsupported_reason(kind, item_count=len(items))
        age = entry.age(self._require_cache().now())
        return ItemSet(
            items=items,
            source=Source.STASH,
            league=league_name,
            tab_index=tab_index,
            tab_name=tab_name,
            unsupported=blocked,
            meta=_cached_meta(entry, note=blocked or f"cached {age:.0f}s ago"),
        )

    async def stash_state(
        self, league: str | None = None, *, refresh: bool = False
    ) -> StashState:
        """What is on disk, how old it is, and what the rest would cost.

        Reads the cache directly and fetches nothing but the tab list. A remove-only
        tab is reported ``permanent`` and never ``stale``, which is what stops a
        surface offering to refresh a tab that cannot change.
        """
        tabs = await self.get_stash_tabs(league, refresh=refresh)
        account_name = await self._account(None)
        cache = self._require_cache()
        now = cache.now()
        default_ttl = float(self._setting("stash_items_ttl_seconds", DEFAULT_STASH_ITEMS_TTL))

        states: list[TabState] = []
        for tab in tabs.all_tabs():
            entry = cache.get(self._items_key(account_name, tabs.league, tab.index))
            ttl = tab_ttl(tab, default_ttl)
            age = entry.age(now) if entry is not None else None
            states.append(
                TabState(
                    tab=tab,
                    cached=entry is not None,
                    fetched_at=_as_datetime(entry.fetched_at) if entry is not None else None,
                    age_seconds=age,
                    stale=age is not None and age >= ttl,
                    permanent=tab.remove_only,
                    item_count=_item_count(entry.payload) if entry is not None else None,
                )
            )
        return StashState(
            league=tabs.league,
            tabs=states,
            meta=tabs.meta,
            seconds_per_request=self.seconds_per_item_request(),
            buckets=self.item_budgets(),
        )

    def item_budgets(self) -> list[Budget]:
        """Every learned item bucket, for the cost estimate.

        Two on the measured account — ``30:60`` and ``100:1800`` — and carrying both
        is the difference between "a 15-tab refresh takes 15 seconds" and "it takes
        four and a half minutes". The second is what the sustained rate alone says,
        and it is wrong by an order of magnitude for every small refresh.
        """
        return [
            Budget(max_hits=snapshot.effective_max, period=float(snapshot.period))
            for snapshot in self._require_net().limits()
            if "item" in snapshot.policy and snapshot.effective_max > 0
        ]

    def seconds_per_item_request(self) -> float:
        """The sustained item-request interval, learned where the limiter has learned it.

        Estimation only — pacing is header-driven and lives in `net`. Reading it off
        the real buckets is what lets a cost warning shrink as the cache fills instead
        of quoting a constant from a research note forever.
        """
        worst = 0.0
        for snapshot in self._require_net().limits():
            if "item" not in snapshot.policy or snapshot.effective_max <= 0:
                continue
            worst = max(worst, snapshot.period / snapshot.effective_max)
        return worst or MEASURED_ITEM_INTERVAL

    async def crawl_stash(
        self,
        league: str | None = None,
        *,
        resume: bool = True,
        limit: int | None = None,
        refresh: bool = False,
    ) -> AsyncIterator[CrawlStep]:
        """Walk every readable tab, one request at a time, resumably.

        **Nothing calls this on a timer, on start, or on a zone change.** It exists to
        be pressed. Progress is written to disk after every tab, so a crawl
        interrupted at minute 28 resumes at tab 94 rather than at tab 1.
        """
        league_name = await self._league(league)
        account_name = await self._account(None)
        tabs = await self.get_stash_tabs(league_name, refresh=refresh)
        progress = self._load_progress(account_name, league_name)
        if not resume or progress is None:
            progress = CrawlProgress(league=league_name, started_at=utcnow())

        done = set(progress.done)
        queue = [
            tab for tab in tabs.all_tabs() if tab.supported and not (resume and tab.index in done)
        ]
        if limit is not None:
            queue = queue[: max(0, limit)]

        total = len(queue)
        for position, tab in enumerate(queue, start=1):
            before = self._requests_made
            try:
                items = await self.get_stash_items(tab.index, league_name, refresh=refresh)
            except PoeApiError as exc:
                progress = progress.model_copy(
                    update={
                        "failed": sorted({*progress.failed, tab.index}),
                        "updated_at": utcnow(),
                    }
                )
                self._save_progress(account_name, progress)
                yield CrawlStep(tab=tab, index=position, total=total, error=str(exc))
                continue
            spent = max(0, self._requests_made - before)
            progress = progress.model_copy(
                update={
                    "done": sorted({*progress.done, tab.index}),
                    "failed": [i for i in progress.failed if i != tab.index],
                    "updated_at": utcnow(),
                    "requests": progress.requests + spent,
                    "items": progress.items + len(items.items),
                }
            )
            self._save_progress(account_name, progress)
            yield CrawlStep(
                tab=tab, index=position, total=total, items=items, from_cache=spent == 0
            )

    async def crawl_progress(self, league: str | None = None) -> CrawlProgress | None:
        """The bookmark an interrupted crawl left behind, or ``None``.

        Spends nothing: it reads one file. It is ``async`` only because the account
        name is resolved the same way every other stash key resolves it — a bookmark
        filed under a different account than the crawl wrote it under would silently
        make every resumed crawl a fresh one.
        """
        league_name = await self._league(league)
        account_name = await self._account(None)
        return self._load_progress(account_name, league_name)

    async def clear_crawl(self, league: str | None = None) -> None:
        """Forget a crawl's bookmark, so the next one starts from the top."""
        league_name = await self._league(league)
        account_name = await self._account(None)
        self._require_cache_storage().delete(crawl_key(account_name, league_name))

    # -- stash internals -------------------------------------------------------

    @staticmethod
    def _tabs_key(account: str, league: str) -> str:
        return f"stash-tabs:{account}:{league}"

    @staticmethod
    def _items_key(account: str, league: str, tab_index: int) -> str:
        return f"stash-items:{account}:{league}:{tab_index}"

    def _remember_tabs(self, account: str, league: str, payload: Any) -> None:
        """File the ``tabs`` array a stash response carried, if it carried one."""
        if not isinstance(payload, Mapping) or not isinstance(payload.get("tabs"), list):
            return
        self._require_cache().put(self._tabs_key(account, league), payload)

    def _known_tab(self, account: str, league: str, tab_index: int) -> StashTab | None:
        """The tab's metadata from the cached tab list, or ``None``.

        Deliberately cache-only. Falling back to a fetch here would make every stash
        read cost two requests on a cold cache — and the answer is only ever used to
        make a fetch *cheaper* or to refuse one, so not knowing degrades to the
        ordinary path rather than to a wrong one.
        """
        entry = self._require_cache().get(self._tabs_key(account, league))
        if entry is None:
            return None
        for tab in tabs_from(entry.payload):
            for candidate in flatten([tab]):
                if candidate.index == tab_index:
                    return candidate
        return None

    def _load_progress(self, account: str, league: str) -> CrawlProgress | None:
        raw = self._require_cache_storage().read_json(crawl_key(account, league))
        if not isinstance(raw, dict):
            return None
        try:
            return CrawlProgress.model_validate(raw)
        except ValidationError:
            # A bookmark that will not parse is a bookmark we ignore: starting over
            # costs requests, but acting on a half-decoded one could skip real tabs.
            self._log().warning("discarding an unreadable stash crawl bookmark")
            return None

    def _save_progress(self, account: str, progress: CrawlProgress) -> None:
        self._require_cache_storage().write_json(
            crawl_key(account, progress.league), progress.to_json()
        )

    def limits(self) -> list[dict[str, Any]]:
        return [snapshot.to_json() for snapshot in self._require_net().limits()]

    # -- JSON wrappers for the method registry ---------------------------------

    async def get_profile_json(self, refresh: bool = False) -> dict[str, Any]:
        return (await self.get_profile(refresh=refresh)).to_json()

    async def get_characters_json(self, refresh: bool = False) -> dict[str, Any]:
        return (await self.get_characters(refresh=refresh)).to_json()

    async def get_items_json(
        self, character: str | None = None, refresh: bool = False
    ) -> dict[str, Any]:
        return (await self.get_items(character, refresh=refresh)).to_json()

    async def get_stash_tabs_json(
        self, league: str | None = None, refresh: bool = False
    ) -> dict[str, Any]:
        return (await self.get_stash_tabs(league, refresh=refresh)).to_json()

    async def get_stash_items_json(
        self, tab_index: int, league: str | None = None, refresh: bool = False
    ) -> dict[str, Any]:
        return (await self.get_stash_items(tab_index, league, refresh=refresh)).to_json()

    async def stash_state_json(
        self, league: str | None = None, refresh: bool = False
    ) -> dict[str, Any]:
        return (await self.stash_state(league, refresh=refresh)).to_json()

    async def limits_json(self) -> list[dict[str, Any]]:
        return self.limits()

    # -- the one fetch path ----------------------------------------------------

    async def _fetch(
        self,
        *,
        path: str,
        route: str,
        params: Mapping[str, Any],
        cache_key: str,
        ttl: float,
        refresh: bool,
        min_interval: float = 0.0,
    ) -> tuple[Any, Meta]:
        """Cache, fetch, or degrade — and say which happened.

        Returns the raw payload plus the :class:`Meta` describing its freshness.
        Normalization happens in the caller, because a cached payload and a fresh one
        must go through exactly the same code.
        """
        cache = self._require_cache()
        entry = cache.get(cache_key)
        age = entry.age(cache.now()) if entry else None

        if entry is not None and age is not None:
            if not refresh and age < ttl:
                return entry.payload, _cached_meta(entry, note=f"cached {age:.0f}s ago")
            if refresh and age < min_interval:
                # The floor `refresh=True` cannot cross.
                return entry.payload, _cached_meta(
                    entry,
                    note=(
                        f"refresh ignored: this endpoint is limited to one call per "
                        f"{min_interval:.0f}s"
                    ),
                    retry_after=min_interval - age,
                )

        try:
            payload = await self._require_net().get_json(path, params=params, route=route)
        except AuthRejected as exc:
            raise SessionRejectedError(await self._reject(exc)) from None
        except RateLimited as exc:
            if entry is not None:
                self._log().info(
                    "%s refused for %.0fs; serving cache from %.0fs ago",
                    path,
                    exc.retry_after,
                    entry.age(cache.now()),
                )
                return entry.payload, _cached_meta(
                    entry,
                    stale=True,
                    retry_after=exc.retry_after,
                    note=exc.reason or "rate limited",
                )
            raise RateLimitedError(exc.retry_after, exc.reason) from None
        except HttpStatusError as exc:
            if entry is not None:
                return entry.payload, _cached_meta(
                    entry, stale=True, note=f"HTTP {exc.status}; showing cached data"
                )
            raise PoeApiError(str(exc)) from None
        except NetError as exc:
            if entry is not None:
                return entry.payload, _cached_meta(
                    entry, stale=True, note=f"{type(exc).__name__}; showing cached data"
                )
            raise PoeApiError(str(exc)) from None

        self._requests_made += 1
        stored = cache.put(cache_key, payload)
        await self._accept()
        return payload, Meta(fetched_at=_as_datetime(stored.fetched_at))

    # -- internals -------------------------------------------------------------

    async def _character(self, explicit: str | None) -> tuple[str, CharacterList | None]:
        """Which character to read, and the roster it was read from if we fetched one.

        The roster is handed back rather than re-fetched by the caller so that
        resolving *both* the default character and its league costs the one
        ``get-characters`` call — the tightest endpoint on the account.

        Precedence: explicit argument, then ``POEDEX_CHARACTER`` (one process,
        set by a flag), then the persisted ``poeapi.character`` setting, then
        whoever was played most recently. A named character short-circuits the
        roster fetch entirely; only the fallback needs it.
        """
        if explicit and explicit.strip():
            return explicit.strip(), None
        chosen = os.environ.get(CHARACTER_ENV) or str(self._setting("character", ""))
        if chosen.strip():
            return chosen.strip(), None
        roster = await self.get_characters()
        current = roster.current()
        if current is None:
            raise PoeApiError("the account has no characters in any league")
        return current.name, roster

    async def _character_league(self, name: str, roster: CharacterList | None) -> str | None:
        """The league ``name`` is playing in, or ``None`` if it cannot be had.

        Not an error: a bag is still a bag without its league, and the honest
        failure belongs to whoever needs the league (``prices`` raises
        :class:`LeagueUnknownError`). What this must never do is *substitute* one —
        that is the bug this whole path exists to close.

        Costs no request: the caller has already resolved the roster, which
        ``get-characters`` caches for an hour and the default-character path has
        usually just fetched. ``None`` means it could not be reached at all.
        """
        if roster is None:
            return None
        entry = roster.named(name)
        if entry is None:
            self._log().warning(
                "no character named %r on this account; its league is unknown and "
                "nothing downstream will guess one",
                name,
            )
            return None
        if not entry.league:
            self._log().warning("the API returned no league for %r", name)
            return None
        return entry.league

    async def _roster(self) -> CharacterList | None:
        """The character list, or ``None`` when it is momentarily unavailable.

        Swallowed on purpose. A rate-limited or failed ``get-characters`` must not
        turn a working ``get-items`` into an error; it turns the *league* into
        ``None``, and the module that needs a league says so loudly.
        """
        try:
            return await self.get_characters()
        except PoeApiError as exc:
            self._log().info("character list unavailable (%s); league unresolved", exc)
            return None

    async def _account(self, explicit: str | None) -> str:
        """The account name ``get-items`` needs, in order of authority.

        Argument, then the ``poeapi.account`` setting, then the name filed with the
        credential, then **the session itself**. The last rung is the one that makes
        the Deck work: the first three all need somebody to have typed a name, and
        the LAN pairing form collects a code and a credential — so before it existed,
        a Deck could pair successfully and then fail every request forever.

        The two stated ones stay on top, because a derived answer can be the wrong
        one — a second account, a realm this build guesses badly — and an override
        that a lookup could beat is not an override.
        """
        if explicit and explicit.strip():
            return explicit.strip()
        configured = str(self._setting("account", "")).strip()
        if configured:
            return configured
        if self._credentials is not None:
            status = await self._credentials.status()
            if status.account:
                return status.account
        derived, why = await self._account_from_profile()
        if derived:
            return derived
        raise AccountUnknownError(
            f"the account name could not be read from the session: {why}. It is "
            "normally taken from your profile and stored with the credential; if "
            "that keeps failing, name it yourself with "
            "'poedex config set poeapi.account <name>'."
        )

    async def _resolve_account(self) -> str | None:
        """The :class:`~modules.credentials.backend.api.AccountResolver` `credentials`
        is handed at start, and the only caller that passes ``refresh=True``.

        Fresh on purpose: this runs when a *new* credential has just been stored, and
        a day-old cached profile is quite likely to name the account the old cookie
        belonged to. ``None`` for every failure — a pair must not die because a
        lookup did.
        """
        try:
            return (await self.get_profile(refresh=True)).account
        except PoeApiError:
            return None

    async def _account_from_profile(self) -> tuple[str | None, str]:
        """The account behind the current session, and why not when there is no name.

        Files what it learns with the credential, so this costs one request per
        session rather than one per call: the next :meth:`_account` finds it on the
        rung above and never gets here. The response cache would already make a
        second request unnecessary, but the record is what survives a restart and
        what ``poedex auth status`` prints.

        :class:`SessionRejectedError` is re-raised rather than folded into a "could
        not read the account" message. A dead session and a missing name are
        different problems with different fixes, and the whole point of deriving the
        name was to stop those two arriving as the same 403.
        """
        try:
            profile = await self.get_profile()
        except SessionRejectedError:
            raise
        except PoeApiError as exc:
            return None, str(exc)
        if self._credentials is not None:
            await self._credentials.mark_ok(profile.account)
        return profile.account, ""

    async def _league(self, explicit: str | None) -> str:
        """Which league's stash to read: argument, then setting, then the character.

        The stash endpoint *requires* a league in the query string, so unlike a bag
        there is no "carry on without one" — but the answer is still never invented.
        """
        if explicit and explicit.strip():
            return explicit.strip()
        configured = str(self._setting("league", NO_LEAGUE)).strip()
        if configured:
            return configured
        roster = await self._roster()
        current = roster.current() if roster is not None else None
        if current is not None and current.league:
            return current.league
        raise LeagueUnknownError(
            "no league to read the stash from: pass one, or run "
            "'poedex config set poeapi.league <league>'. Reading Standard by default "
            "is how a tool shows you somebody else's stash and calls it yours."
        )

    def _configured_realm(self, explicit: str | None) -> str | None:
        """The realm somebody *stated*: the argument, then the setting. Else ``None``.

        Split out from :meth:`_realm` because ``get-characters`` may use only this
        half — asking the roster which realm to fetch the roster from is the circle
        the old ``REALM = "pc"`` constant papered over.
        """
        if explicit and explicit.strip():
            return explicit.strip()
        configured = str(self._setting("realm", NO_REALM)).strip()
        return configured or None

    async def _realm(
        self,
        explicit: str | None,
        *,
        character: str | None = None,
        roster: CharacterList | None = None,
        may_fetch: bool = True,
    ) -> str | None:
        """Which realm to ask about: argument, then setting, then the roster entry.

        ``None`` is a real answer and means "nobody can say". The caller then leaves
        the parameter off the request entirely — it does not fall back to ``pc``,
        because that is precisely the constant this replaced. The fallback is loud:
        every unresolved realm logs a warning naming the command that fixes it.

        The roster entry is the authority, and it is the same entry the league comes
        from. ``character`` picks the row when the caller knows which character it is
        reading; the stash, which has no character, takes the account's current one.

        ``may_fetch=False`` says the caller has already tried and ``roster=None``
        means *unavailable* rather than *not looked up* — so a failed character
        fetch is not immediately retried while the limiter is still backing off.
        """
        stated = self._configured_realm(explicit)
        if stated:
            return stated
        if roster is None and may_fetch:
            roster = await self._roster()
        if roster is None:
            self._warn_realm("the character list could not be reached")
            return None
        entry = roster.named(character) if character else roster.current()
        if entry is None:
            self._warn_realm(f"no character named {character!r} on this account")
            return None
        if not entry.realm:
            self._warn_realm("the character list did not say which realm it is on")
            return None
        return entry.realm

    def _warn_realm(self, reason: str) -> None:
        self._log().warning(
            "cannot tell which realm this account is on (%s); the realm parameter is "
            "being left off the request, so GGG answers for whichever realm it "
            "defaults to. On an Xbox or Sony account that is the wrong one: run "
            "'poedex config set poeapi.realm <pc|xbox|sony>'.",
            reason,
        )

    def _setting(self, key: str, default: Any) -> Any:
        if self._ctx is None:
            return default
        return self._ctx.settings.get(key, default)

    async def _accept(self) -> None:
        """Record that the session still works — but not on every single request.

        ``mark_ok`` writes the session file and emits ``credential_changed``. Doing
        that on every sync means a disk write and a UI notification per zone
        transition, to record something that has not changed. Once every few minutes
        is enough to keep the "last confirmed" timestamp honest.
        """
        if self._credentials is None:
            return
        status = await self._credentials.status()
        if status.state is CredentialState.OK and status.last_ok_at is not None:
            age = (datetime.now(UTC) - status.last_ok_at).total_seconds()
            if age < MARK_OK_INTERVAL:
                return
        await self._credentials.mark_ok()

    async def _reject(self, exc: AuthRejected) -> str:
        """Tell `credentials` first, then describe the situation truthfully.

        "Your session expired" and "you never paired" are different problems with
        different fixes, and showing the first when the second is true is how a user
        concludes the tool is broken. The distinction is available — `credentials`
        knows whether anything is stored — so it gets made.
        """
        if self._credentials is None:  # pragma: no cover - only without a context
            return "the API rejected the request"
        before = await self._credentials.status()
        if before.state is CredentialState.NEVER_SET:
            return "no credential is stored. Run 'poedex auth set'."
        # The note is built from a status code and a path, never from the value.
        await self._credentials.mark_rejected(f"HTTP {exc.status} from {exc.path}")
        return "the API rejected the stored session; it has expired or was revoked"

    async def _announce(self, key: str, result: ItemSet) -> None:
        """Emit ``sync_complete`` with the normalized hash (SPEC §4.4).

        The hash is over normalized items, so "unchanged" means the bag really is
        unchanged rather than that the bytes happened to match.
        """
        if self._ctx is None or result.meta.stale or result.meta.from_cache:
            return
        digest = result.content_hash
        changed = self._hashes.get(key) != digest
        self._hashes[key] = digest
        await self._ctx.events.emit(
            SYNC_COMPLETE,
            {
                "key": key,
                "source": result.source.value,
                "items": len(result.items),
                "content_hash": digest,
                "changed": changed,
                "fetched_at": result.meta.fetched_at.isoformat(),
            },
            source=self.id,
        )

    def _log(self) -> Any:
        return self._ctx.logger if self._ctx else _fallback_log

    def _require_net(self) -> NetApi:
        if self._net is None:
            raise ModuleNotStartedError("poeapi has not been started")
        return self._net

    def _require_cache(self) -> ResponseCache:
        if self._cache is None:
            raise ModuleNotStartedError("poeapi has not been started")
        return self._cache

    def _require_cache_storage(self) -> Storage:
        """Where a crawl's bookmark lives — the module's own namespace, beside the
        response cache but not inside it: a cache entry is a copy of something GGG
        said, and this is a note to ourselves about how far we got."""
        if self._storage is None:
            raise ModuleNotStartedError("poeapi has not been started")
        return self._storage

    def __repr__(self) -> str:
        return f"PoeApiModule(started={self._net is not None})"


def _realm_param(params: dict[str, Any], realm: str | None) -> dict[str, Any]:
    """Add ``realm`` to a query only when one is actually known.

    An absent parameter and a guessed one are different claims. This is the single
    place that decides which of the two goes on the wire, so there is nowhere left
    for a ``"pc"`` to be reintroduced by accident.
    """
    if realm:
        params["realm"] = realm
    return params


# -- payload readers ------------------------------------------------------------
#
# Kept as free functions so a test can feed them a fixture without a module.


def _profile_name(payload: Any) -> str | None:
    """``/api/profile`` answers a single object; the account is its ``name``.

    Verified against the live account: ``{"uuid": ..., "name": "Name#1234", ...}``,
    where the discriminator is part of the name and is passed through untouched.
    ``strip_set_tokens`` for the same reason every other name goes through it —
    GGG's own markup can appear in a name field and must never reach a query string.
    """
    if not isinstance(payload, Mapping):
        return None
    return strip_set_tokens(payload.get("name")) or None


def _profile_uuid(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("uuid")
    return value if isinstance(value, str) and value else None


def _characters_from(payload: Any) -> list[Character]:
    """``get-characters`` returns a bare array."""
    entries = payload if isinstance(payload, list) else []
    out: list[Character] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        name = strip_set_tokens(entry.get("name"))
        if not name:
            continue
        out.append(
            Character(
                name=name,
                league=entry.get("league") if isinstance(entry.get("league"), str) else None,
                realm=entry.get("realm") if isinstance(entry.get("realm"), str) else None,
                class_name=entry.get("class") if isinstance(entry.get("class"), str) else None,
                level=_int(entry.get("level")),
                experience=_int(entry.get("experience")),
                current=bool(entry.get("current", False)),
            )
        )
    return out


def _kind_in(payload: Any, tab_index: int) -> TabKind:
    """The tab's kind as reported by the response that carried its items.

    Every ``get-stash-items`` call asks for ``tabs=1``, so the tab list rides along
    with the items for free. That is what makes a cold read of one tab still able to
    tell a map tab from a premium one without a second request.
    """
    if not isinstance(payload, Mapping):
        return TabKind.UNKNOWN
    entries = payload.get("tabs")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, Mapping) and _int(entry.get("i"), -1) == tab_index:
                return tab_kind(str(entry.get("type") or ""))
    return TabKind.UNKNOWN


def _item_count(payload: Any) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    items = payload.get("items")
    return len(items) if isinstance(items, list) else None


def _tab_name(payload: Any, tab_index: int) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    entries = payload.get("tabs")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, Mapping) and _int(entry.get("i"), -1) == tab_index:
                return strip_set_tokens(entry.get("n") or entry.get("name")) or None
    return None


def _cached_meta(
    entry: CacheEntry,
    *,
    stale: bool = False,
    retry_after: float | None = None,
    note: str | None = None,
) -> Meta:
    return Meta(
        fetched_at=_as_datetime(entry.fetched_at),
        from_cache=True,
        stale=stale,
        retry_after=retry_after,
        note=note,
    )


def _as_datetime(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


MODULE = PoeApiModule()
