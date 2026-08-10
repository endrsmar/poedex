"""The `appraisal` module — prices become verdicts.

Feature, not core, for the same reason `prices` is (IMPLEMENTATION-PLAN §1.3), only
more so: this module is nothing *but* opinion. What "worth keeping" means, how
suspicious to be about a rare, which bases are worth something blank, whether a
missing price is a hole or a non-question — none of that is infrastructure, and a
core module holding it is how a contained core stops being contained.

## `requires` is `["prices", "poeapi"]`, and the plan says `["prices"]`

A stated deviation, with the same shape as Phase 3's (`prices` requires `net` as well
as `poeapi`, against §1.3's arrow diagram). Two reasons:

1. **The API is defined over `poeapi`'s types.** ``AppraisalApi.appraise`` takes
   ``NormalizedItem`` — SPEC §4.5's boundary, and `poeapi`'s model. Importing it
   through `prices`' ``api.py`` because it happens to be re-exported there would be
   a dependency on `prices`' import list, which is not a public surface.
2. **Without it, ``appraisal.appraise_bag`` cannot exist.** ``PricesApi`` has no bag
   accessor, so the only alternative is a method that takes the whole normalized bag
   as an argument — meaning the Phase 5 bag screen fetches the bag to the browser
   and posts it back to be judged. That is a round trip of data the backend already
   holds, and it hands the frontend the ability to submit items it invented, which
   is precisely the hole ``prices.quote_json`` refuses to open by taking a uid.

`poeapi` is core and `prices` is a feature, so both edges are legal in either
direction of the kind rule; the registry and the boundary tests check that, and both
run against this module from the moment it lands.

## What this module does not do

It does not price anything. There is no poe.ninja parsing here, no trade query
building, no currency conversion — every number comes from ``PricesApi`` and is
carried through untouched, including the ``unpriceable`` state, which arrives as a
distinct outcome and leaves as a distinct verdict.

## What it now *does* do: decide when tier 3 runs

SPEC §5.3 said "never eager". That rule was written against **stash** scale, where a
generous gate over 818 items produces hundreds of candidates and eager escalation is
a self-inflicted rate-limit violation. A bag is not a stash: the real account's
backpack holds three to five rares, and ``trade-search-request-limit`` is
``5:10:60``. Four searches and four fetches fit inside it with room left over.

So the rule is now parameterised by the thing that already encodes exactly this
distinction — :class:`Strictness`.

* **generous (bag)** — gate-passing rares are quoted eagerly, up to
  ``max_eager_quotes``, bounded by ``eager_timeout_seconds``. Anything still in
  flight when the timeout expires comes back marked ``pricing…`` rather than
  delaying the output; the bag total is then a floor and says so.
* **strict (stash)** — never. Unchanged, and the setting cannot override it.

`prices` did not gain an eager path: ``PricesApi.quote_many`` is explicit, bounded,
and this module is its only caller. The decision about *when* spending a shared
budget is appropriate is a feature opinion, which is why it lives here and not in
the module that owns the socket.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from modules.appraisal.backend.api import (
    APPRAISAL_COMPLETE,
    DEFAULT_CHECK_CHAOS,
    DEFAULT_KEEP_CHAOS,
    AppraisalApi,
    AppraisalError,
    BagAppraisal,
    GateResult,
    ItemVerdict,
    Strictness,
)
from modules.appraisal.backend.gate import HIGH_VALUE_BASES, evaluate
from modules.appraisal.backend.verdict import appraise_bag, appraise_one
from modules.poeapi.backend.api import NormalizedItem, PoeApi, Source
from modules.prices.backend.api import (
    BagValuation,
    Price,
    PricesApi,
    PriceSource,
    Tier3,
)
from runtime.context import ModuleContext
from runtime.errors import ModuleNotStartedError
from runtime.log import get_logger

__all__ = ["MODULE", "AppraisalModule"]

_fallback_log = get_logger("module.appraisal")

DEFAULT_STRICTNESS = Strictness.GENEROUS
"""The bag is the default surface and the bag wants the generous gate (SPEC §5.2)."""

DEFAULT_MAX_EAGER = 4
"""Gate-passing rares priced per bag pass. Two requests each — one search, one
fetch — against a measured ``5:10:60, 15:60:300`` search budget on an IP the
player's own browser session also uses. Four is the largest number that still looks
like a person using a website."""

DEFAULT_EAGER_TIMEOUT = 12.0
"""Seconds the pass will wait for tier 3 before showing ``pricing…`` instead."""


class AppraisalModule:
    id = "appraisal"
    name = "Appraisal"
    kind = "feature"
    requires: ClassVar[list[str]] = ["poeapi", "prices"]
    """See the module docstring: the plan's §1.4 sketch says ``["prices"]``, and the
    `poeapi` edge is a deliberate, stated addition rather than an oversight."""

    provides: type | None = AppraisalApi

    def __init__(self) -> None:
        self._ctx: ModuleContext | None = None
        self._prices: PricesApi | None = None
        self._poeapi: PoeApi | None = None

    # -- lifecycle -------------------------------------------------------------

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        self._prices = ctx.require(PricesApi)
        self._poeapi = ctx.require(PoeApi)
        ctx.logger.info(
            "appraisal ready: keep >= %gc, gate %s",
            self.threshold(),
            self.strictness().value,
        )

    async def stop(self) -> None:
        self._ctx = None
        self._prices = None
        self._poeapi = None

    def methods(self) -> dict[str, Callable[..., Any]]:
        """Registered as ``appraisal.*`` for the Phase 5 HTTP and Phase 7 Decky
        transports. Nothing here takes an item object: a uid or a character name,
        so the frontend cannot submit an item it made up."""
        return {
            "appraise_bag": self.appraise_bag_json,
            "gate": self.gate_json,
            "settings": self.settings_json,
        }

    def settings_schema(self) -> dict[str, Any]:
        return {
            "keep_threshold_chaos": {
                "type": "float",
                "default": DEFAULT_KEEP_CHAOS,
                "min": 0.0,
                "max": 1_000_000.0,
                "label": "Keep threshold (chaos)",
                "description": (
                    "At or above this, an item is 'keep'. SPEC §11 leaves the "
                    "default open: ~20c gives a busy panel, divine-tier a quiet "
                    "one. Appraising your own bag is how you settle it."
                ),
            },
            "check_threshold_chaos": {
                "type": "float",
                "default": DEFAULT_CHECK_CHAOS,
                "min": 0.0,
                "max": 1_000_000.0,
                "label": "Check floor (chaos)",
                "description": (
                    "Below the keep threshold but at or above this is 'check' — "
                    "SPEC §5.4's 'below threshold but non-trivial'. Below it, and "
                    "with nothing from the tier-2 gate, an item is 'trash'."
                ),
            },
            "strictness": {
                "type": "str",
                "default": DEFAULT_STRICTNESS.value,
                "choices": [s.value for s in Strictness],
                "label": "Tier-2 gate strictness",
                "description": (
                    "'generous' for the bag: a false negative tells you to vendor "
                    "something good. 'strict' for the stash: the item is already "
                    "safe, and a generous gate at stash scale is all noise."
                ),
            },
            "eager_tier3": {
                "type": "bool",
                "default": True,
                "label": "Price a bag's rares automatically",
                "description": (
                    "A rare has no bulk price and never will, so without this every "
                    "rare in the bag comes back with a gate opinion and no number. "
                    "Only ever runs for a bag, only for items the gate flagged, and "
                    "at most a few per pass — a stash is never escalated eagerly."
                ),
            },
            "max_eager_quotes": {
                "type": "int",
                "default": DEFAULT_MAX_EAGER,
                "min": 0,
                "max": 10,
                "label": "Rares to price per bag",
                "description": (
                    "One trade search plus one fetch each. The measured limit is 5 "
                    "searches per 10 seconds and 15 per minute, and the player's own "
                    "browser shares that IP — four leaves real headroom."
                ),
            },
            "eager_timeout_seconds": {
                "type": "float",
                "default": DEFAULT_EAGER_TIMEOUT,
                "min": 0.1,
                "max": 60.0,
                "label": "How long to wait for rare prices",
                "description": (
                    "Whatever has not answered by then is shown as 'pricing…' and "
                    "the bag total is reported as a floor. A slow query may cost a "
                    "number; it must never cost the output."
                ),
            },
            "extra_high_value_bases": {
                "type": "list",
                "default": [],
                "label": "Extra high-value bases",
                "description": (
                    "Base types added to the gate's allowlist, exactly as they are "
                    "spelled in game ('Convoking Wand'). The built-in list is short "
                    "on purpose; league-specific bases belong here, not in code."
                ),
            },
        }

    # -- AppraisalApi ----------------------------------------------------------

    def threshold(self) -> float:
        return float(self._setting("keep_threshold_chaos", DEFAULT_KEEP_CHAOS))

    def check_floor(self) -> float:
        return float(self._setting("check_threshold_chaos", DEFAULT_CHECK_CHAOS))

    def strictness(self) -> Strictness:
        raw = str(self._setting("strictness", DEFAULT_STRICTNESS.value))
        try:
            return Strictness(raw)
        except ValueError:
            self._log().warning("unknown strictness %r; using %s", raw, DEFAULT_STRICTNESS.value)
            return DEFAULT_STRICTNESS

    def allowlist(self) -> frozenset[str]:
        extra = self._setting("extra_high_value_bases", [])
        if not isinstance(extra, list):
            return HIGH_VALUE_BASES
        return HIGH_VALUE_BASES | {str(name) for name in extra if str(name).strip()}

    def gate(
        self, item: NormalizedItem, *, strictness: Strictness | None = None
    ) -> GateResult:
        return evaluate(
            item,
            strictness=strictness or self.strictness(),
            allowlist=self.allowlist(),
        )

    async def appraise(
        self,
        items: Sequence[NormalizedItem],
        *,
        strictness: Strictness | None = None,
        threshold_chaos: float | None = None,
        league: str | None = None,
        override: str | None = None,
        escalate: bool | None = None,
    ) -> BagAppraisal:
        rows = list(items)
        level = strictness or self.strictness()
        keep = self.threshold() if threshold_chaos is None else float(threshold_chaos)
        valued = await self._require_prices().value_all(rows, league=league, override=override)
        gates = [self.gate(item, strictness=level) for item in rows]
        if self._should_escalate(level, escalate):
            valued = await self._escalate(rows, valued, gates, league=league, override=override)
        return appraise_bag(
            rows,
            valued,
            gates,
            keep_chaos=keep,
            check_chaos=min(self.check_floor(), keep),
            strictness=level,
        )

    def _should_escalate(self, level: Strictness, explicit: bool | None) -> bool:
        """Whether this pass may spend trade requests.

        Strictness is checked **after** the explicit argument and cannot be
        overridden by it. ``--escalate`` on a stash run is a request to do something
        SPEC §5.3 forbids for a reason that has not changed: hundreds of candidates
        against a five-per-ten-seconds budget.
        """
        if level is not Strictness.GENEROUS:
            return False
        if explicit is not None:
            return bool(explicit)
        return bool(self._setting("eager_tier3", True))

    async def _escalate(
        self,
        items: Sequence[NormalizedItem],
        valued: BagValuation,
        gates: Sequence[GateResult],
        *,
        league: str | None,
        override: str | None,
    ) -> BagValuation:
        """Tier 3 over the bag's gated rares, bounded, then folded back in.

        Only rows that are **unpriceable and gated** are candidates. A rare with a
        note already has the player's own number; an item bulk priced does not need
        a search; an item the gate ignored is one the gate has already said is not
        worth a request.
        """
        limit = max(0, int(self._setting("max_eager_quotes", DEFAULT_MAX_EAGER)))
        if not limit:
            return valued
        candidates = [
            (position, item)
            for position, (item, gate) in enumerate(zip(items, gates, strict=True))
            if gate.passed and valued.items[position].unpriceable
        ]
        if not candidates:
            return valued
        # Most signals first: with a budget of four and five candidates, the one left
        # out should be the least interesting, not the last one in the bag.
        candidates.sort(key=lambda pair: -len(gates[pair[0]].signals))
        chosen = candidates[:limit]

        prices = self._require_prices()
        before = prices.trade_requests
        quotes = await prices.quote_many(
            [item for _position, item in chosen],
            league=league or override,
            timeout=float(self._setting("eager_timeout_seconds", DEFAULT_EAGER_TIMEOUT)),
            # The gate just decided why each of these is interesting. Handing that
            # down is what stops the query from ANDing every mod on the item — see
            # `GateResult.focus`.
            focus={item.uid: gates[position].focus() for position, item in chosen},
        )
        rows = list(valued.items)
        for position, item in chosen:
            quote = quotes.get(item.uid)
            if quote is None:
                # Absent means the task was still running when the timeout fired.
                # This is the *only* case that is genuinely "pricing…".
                rows[position].tier3 = Tier3.PENDING
                continue
            if not quote.searched:
                rows[position].tier3 = Tier3.FAILED
                rows[position].reason = quote.unavailable
                continue
            if quote.chaos is None:
                # The search ran — twice, if the first was empty — and matched
                # nothing. A finished answer, and the surface must be able to stop
                # spinning on it.
                rows[position].tier3 = Tier3.NO_LISTINGS
                rows[position].reason = quote.query or None
                continue
            rows[position] = rows[position].replace_price(
                Price(
                    quote.chaos,
                    PriceSource.TRADE,
                    detail=(
                        f"median of {len(quote.listings)} online listing(s) "
                        f"of {quote.total} matching · {quote.query}"
                    ),
                    listing_count=quote.total or None,
                    sample_size=len(quote.listings) or None,
                ),
            )
        return BagValuation(
            rows,
            league=valued.league,
            league_source=valued.league_source,
            divine_rate=valued.divine_rate,
            table=valued.table,
            lookups=valued.lookups,
            trade_requests=max(0, prices.trade_requests - before),
            exchange_requests=valued.exchange_requests,
        )

    async def appraise_item(
        self,
        item: NormalizedItem,
        *,
        strictness: Strictness | None = None,
        league: str | None = None,
        override: str | None = None,
    ) -> ItemVerdict:
        level = strictness or self.strictness()
        keep = self.threshold()
        valuation = await self._require_prices().value(item, league=league, override=override)
        return appraise_one(
            item,
            valuation,
            self.gate(item, strictness=level),
            keep_chaos=keep,
            check_chaos=min(self.check_floor(), keep),
        )

    # -- JSON wrappers for the method registry ---------------------------------

    async def appraise_bag_json(
        self,
        character: str | None = None,
        strictness: str | None = None,
        threshold_chaos: float | None = None,
        escalate: bool | None = None,
    ) -> dict[str, Any]:
        prices = self._require_prices()
        bag = await self._require_poeapi().get_items(character)
        # Resolving first means a bag with no league fails here, with a message about
        # leagues, rather than four screens later as "everything is unpriceable".
        choice = prices.league_choice(bag.league)
        await prices.ensure_tables(choice.league)
        result = await self.appraise(
            bag.by_source(Source.BAG),
            strictness=_parse_strictness(strictness),
            threshold_chaos=threshold_chaos,
            league=bag.league,
            escalate=escalate,
        )
        await self._announce(result, character=bag.character)
        payload = result.to_json()
        payload["character"] = bag.character
        payload["stale"] = bag.meta.stale
        return payload

    async def gate_json(
        self, uid: str, character: str | None = None, strictness: str | None = None
    ) -> dict[str, Any]:
        """Tier 2 for one item of the current bag, by uid. No pricing, no requests
        beyond the bag fetch `poeapi` may already have cached."""
        bag = await self._require_poeapi().get_items(character)
        for item in bag.items:
            if item.uid == uid:
                return self.gate(item, strictness=_parse_strictness(strictness)).to_json()
        raise AppraisalError(f"no item {uid!r} in the current bag")

    async def settings_json(self) -> dict[str, Any]:
        return {
            "keep_threshold_chaos": self.threshold(),
            "check_threshold_chaos": self.check_floor(),
            "strictness": self.strictness().value,
            "high_value_bases": sorted(self.allowlist()),
        }

    # -- internals -------------------------------------------------------------

    async def _announce(self, result: BagAppraisal, *, character: str | None) -> None:
        if self._ctx is None:
            return
        await self._ctx.events.emit(
            APPRAISAL_COMPLETE,
            {
                "character": character,
                "league": result.league,
                "league_source": (
                    result.league_source.value if result.league_source else None
                ),
                "counts": result.counts,
                "total_chaos": round(result.total_chaos, 4),
                "unpriceable_stack": result.unpriceable_stack,
                "pricing_count": len(result.pricing),
                "total_is_floor": result.total_is_floor,
                "trade_requests": result.trade_requests,
                "threshold_chaos": result.threshold_chaos,
                "strictness": result.strictness.value,
            },
            source=self.id,
        )

    def _setting(self, key: str, default: Any) -> Any:
        if self._ctx is None:
            return default
        return self._ctx.settings.get(key, default)

    def _log(self) -> Any:
        return self._ctx.logger if self._ctx else _fallback_log

    def _require_prices(self) -> PricesApi:
        if self._prices is None:
            raise ModuleNotStartedError("appraisal has not been started")
        return self._prices

    def _require_poeapi(self) -> PoeApi:
        if self._poeapi is None:
            raise ModuleNotStartedError("appraisal has not been started")
        return self._poeapi

    def __repr__(self) -> str:
        return f"AppraisalModule(keep>={self.threshold():g}c, {self.strictness().value})"


def _parse_strictness(raw: str | None) -> Strictness | None:
    if raw is None:
        return None
    try:
        return Strictness(raw)
    except ValueError:
        raise AppraisalError(
            f"unknown strictness {raw!r}; expected one of {[s.value for s in Strictness]}"
        ) from None


MODULE = AppraisalModule()
