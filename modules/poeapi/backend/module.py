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
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

from modules.credentials.backend.api import CredentialsApi, CredentialState
from modules.net.backend.api import AuthRejected, HttpStatusError, NetApi, NetError, RateLimited
from modules.poeapi.backend.api import (
    CHARACTER_ENV,
    CHARACTERS_PATH,
    ITEMS_PATH,
    STASH_PATH,
    SYNC_COMPLETE,
    AccountUnknownError,
    Character,
    CharacterList,
    ItemSet,
    LeagueUnknownError,
    Meta,
    PoeApi,
    PoeApiError,
    RateLimitedError,
    SessionRejectedError,
    Source,
    StashTab,
    StashTabList,
)
from modules.poeapi.backend.cache import CacheEntry, ResponseCache
from modules.poeapi.backend.normalize import normalize_items, strip_set_tokens
from runtime.context import ModuleContext
from runtime.errors import ModuleNotStartedError
from runtime.log import get_logger

__all__ = ["MODULE", "PoeApiModule"]

_fallback_log = get_logger("module.poeapi")

# `get-items` and `get-stash-items` share `backend-item-request-limit`, so they share
# buckets automatically once the policy is learned. The route names below only decide
# which *seed* budget applies before the first response teaches us anything, which is
# why the two item endpoints share one and characters gets its own.
ITEM_ROUTE = "character-window:items"
CHARACTER_ROUTE = "character-window:characters"

# `refresh=True` cannot go below this on the character endpoint. SPEC §4.4: cache
# hard, never poll.
CHARACTERS_MIN_INTERVAL = 60.0

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

    # -- lifecycle -------------------------------------------------------------

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        self._net = ctx.require(NetApi)
        self._credentials = ctx.require(CredentialsApi)
        if self._cache is None:
            self._cache = ResponseCache(ctx.storage)
        configured = str(self._setting("league", NO_LEAGUE)).strip()
        ctx.logger.info(
            "poeapi ready: league=%s",
            configured or "unset (read from the character being synced)",
        )

    async def stop(self) -> None:
        self._ctx = None
        self._net = None
        self._credentials = None

    def methods(self) -> dict[str, Callable[..., Any]]:
        return {
            "get_characters": self.get_characters_json,
            "get_items": self.get_items_json,
            "get_stash_tabs": self.get_stash_tabs_json,
            "get_stash_items": self.get_stash_items_json,
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
                    "Required by get-items. Falls back to the name stored with the "
                    "credential; set it here to override."
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
            cache_key=f"stash-tabs:{account_name}:{league_name}",
            ttl=float(self._setting("stash_tabs_ttl_seconds", DEFAULT_STASH_TABS_TTL)),
            refresh=refresh,
        )
        return StashTabList(league=league_name, tabs=_tabs_from(payload), meta=meta)

    async def get_stash_items(
        self,
        tab_index: int,
        league: str | None = None,
        *,
        refresh: bool = False,
        realm: str | None = None,
    ) -> ItemSet:
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
                    "tabs": 0,
                    "tabIndex": tab_index,
                },
                realm_name,
            ),
            cache_key=f"stash-items:{account_name}:{league_name}:{tab_index}",
            ttl=float(self._setting("stash_items_ttl_seconds", DEFAULT_STASH_ITEMS_TTL)),
            refresh=refresh,
        )
        raw_items = payload.get("items") if isinstance(payload, Mapping) else None
        tab_name = _tab_name(payload, tab_index)
        items = normalize_items(
            raw_items or [],
            source=Source.STASH,
            tab_index=tab_index,
            tab_name=tab_name,
        )
        result = ItemSet(
            items=items,
            source=Source.STASH,
            league=league_name,
            tab_index=tab_index,
            tab_name=tab_name,
            meta=meta,
        )
        await self._announce(f"stash:{league_name}:{tab_index}", result)
        return result

    def limits(self) -> list[dict[str, Any]]:
        return [snapshot.to_json() for snapshot in self._require_net().limits()]

    # -- JSON wrappers for the method registry ---------------------------------

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

        There is no way to look this up: ``get-characters`` does not return it, and
        an account name that is merely *wrong* produces the same 403 as an expired
        session. So it is asked for, and its absence is a distinct error with an
        instruction attached rather than a mystery auth failure.
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
        raise AccountUnknownError(
            "no account name on record. Run 'poedex auth set --account <name>', or "
            "'poedex config set poeapi.account <name>'."
        )

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


def _tabs_from(payload: Any) -> list[StashTab]:
    if not isinstance(payload, Mapping):
        return []
    entries = payload.get("tabs")
    if not isinstance(entries, list):
        return []
    out: list[StashTab] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        name = strip_set_tokens(entry.get("n") or entry.get("name"))
        colour = None
        metadata = entry.get("colour")
        if isinstance(metadata, Mapping):
            red = _int(metadata.get("r")) & 0xFF
            green = _int(metadata.get("g")) & 0xFF
            blue = _int(metadata.get("b")) & 0xFF
            colour = f"#{red:02x}{green:02x}{blue:02x}"
        out.append(
            StashTab(
                index=_int(entry.get("i"), index),
                id=entry.get("id") if isinstance(entry.get("id"), str) else None,
                name=name,
                type=str(entry.get("type") or ""),
                colour=colour,
                hidden=bool(entry.get("hidden", False)),
                # Remove-only tabs are named "(Remove-only) …" by GGG. They can never
                # gain items, which is the highest-leverage caching rule in the
                # feature (research-notes §7) — but acting on it belongs to Phase 3+.
                remove_only="remove-only" in name.casefold(),
            )
        )
    return out


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
