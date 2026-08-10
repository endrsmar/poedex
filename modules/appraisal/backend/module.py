"""The `appraisal` module — prices become verdicts.

Feature, not core, for the same reason `prices` is (IMPLEMENTATION-PLAN §1.3), only
more so: this module is nothing *but* opinion. What "worth keeping" means, how
suspicious to be about a rare, which bases are worth something blank, whether a
missing price is a hole or a non-question — none of that is infrastructure, and a
core module holding it is how a contained core stops being contained.

## `requires` is `["prices", "poeapi", "moddb"]`, and the plan says `["prices"]`

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

The `moddb` edge is Phase 9's, and it is the ordinary feature→core direction. It is
what lets the highlighter say ``T4 of 10 on a helmet, T7 of 13 on a body armour``
instead of comparing one number to one threshold, and what makes the open-affix
option a real filter rather than a guess. Only ``moddb/backend/api.py`` is imported,
which the boundary tests check.

## What it no longer does: spend trade requests unasked

Phase 4b made a bag's gate hits eager: up to ``max_eager_quotes`` tier-3 searches per
appraise, bounded by ``eager_timeout_seconds``. **All of that is deleted.**
IMPLEMENTATION-PLAN §5b records why — it failed twice against a live account, once by
ANDing every mod and matching zero listings, once by matching a single listing and
reporting 10c for an item worth 1.00c across 438 comparables. Narrow gave a missing
answer, wide gave a confidently wrong one, and nothing in between is right, because
the question the query encodes is *which mods make this item interesting* and that is
player knowledge.

So an appraise now makes **one account request and zero trade requests**. What
replaces the eager pass:

* :meth:`AppraisalModule.highlight` — the proposal. Which of the four criteria the
  item hit, its mods as tickable rows with real tiers, and how many affix slots are
  free. Local, synchronous, free.
* :meth:`AppraisalModule.price_check` — the answer, run **only** when a player asks,
  against **their** selection. Two requests.

Bulk pricing stays automatic and untouched: poe.ninja works, is free, is accurate,
and needs no input. The pivot is about rares only.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from modules.appraisal.backend.api import (
    APPRAISAL_COMPLETE,
    DEFAULT_CHECK_CHAOS,
    DEFAULT_KEEP_CHAOS,
    DEFAULT_WIDEN,
    AppraisalApi,
    AppraisalError,
    BagAppraisal,
    GateResult,
    ItemHighlight,
    ItemVerdict,
    PriceCheck,
    Selection,
    Strictness,
)
from modules.appraisal.backend.gate import SOUGHT_AFTER_BASES, evaluate, report_for
from modules.appraisal.backend.highlight import build as build_highlight
from modules.appraisal.backend.verdict import appraise_bag, appraise_one
from modules.moddb.backend.api import ModDbApi, ModDbError
from modules.poeapi.backend.api import NormalizedItem, PoeApi, Source
from modules.prices.backend.api import (
    PricesApi,
    TradeQuote,
    TradeUnavailable,
)
from runtime.context import ModuleContext
from runtime.errors import ModuleNotStartedError
from runtime.log import get_logger

__all__ = ["MODULE", "AppraisalModule"]

_fallback_log = get_logger("module.appraisal")

DEFAULT_STRICTNESS = Strictness.GENEROUS
"""The bag is the default surface and the bag wants the generous gate (SPEC §5.2)."""


class AppraisalModule:
    id = "appraisal"
    name = "Appraisal"
    kind = "feature"
    requires: ClassVar[list[str]] = ["poeapi", "prices", "moddb"]
    """See the module docstring: the plan's §1.4 sketch says ``["prices"]``; the
    `poeapi` edge is Phase 4's stated addition and the `moddb` edge is Phase 9's."""

    provides: type | None = AppraisalApi

    def __init__(self) -> None:
        self._ctx: ModuleContext | None = None
        self._prices: PricesApi | None = None
        self._poeapi: PoeApi | None = None
        self._moddb: ModDbApi | None = None

    # -- lifecycle -------------------------------------------------------------

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        self._prices = ctx.require(PricesApi)
        self._poeapi = ctx.require(PoeApi)
        self._moddb = ctx.require(ModDbApi)
        ctx.logger.info(
            "appraisal ready: keep >= %gc, highlighter %s, %s",
            self.threshold(),
            self.strictness().value,
            self._describe_db(),
        )

    def _describe_db(self) -> str:
        """The mod database's age, said out loud at start.

        A stale mod database does not fail — it answers confidently and wrongly about
        tiers GGG re-levelled — and this module is now the main thing quoting it.
        """
        if self._moddb is None:
            return "no mod database"
        try:
            return self._moddb.version().describe()
        except ModDbError as exc:
            return f"no mod database ({exc})"

    async def stop(self) -> None:
        self._ctx = None
        self._prices = None
        self._poeapi = None
        self._moddb = None

    def methods(self) -> dict[str, Callable[..., Any]]:
        """Registered as ``appraisal.*`` for the Phase 5 HTTP and Phase 7 Decky
        transports. Nothing here takes an item object: a uid or a character name,
        so the frontend cannot submit an item it made up."""
        return {
            "appraise_bag": self.appraise_bag_json,
            "gate": self.gate_json,
            "highlight": self.highlight_json,
            "price_check": self.price_check_json,
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
            "check_widen": {
                "type": "float",
                "default": DEFAULT_WIDEN,
                "min": 0.0,
                "max": 0.9,
                "label": "How far below a ticked roll to search",
                "description": (
                    "0.2 searches '+103 to maximum Life' as '≥ 82'. An exact-value "
                    "filter on a random roll matches almost nothing — that is what "
                    "returned zero listings for two of three rares in a real bag."
                ),
            },
            "extra_sought_after_bases": {
                "type": "list",
                "default": [],
                "label": "Extra sought-after bases",
                "description": (
                    "Base types added to the highlighter's opinion list, exactly as "
                    "they are spelled in game ('Convoking Wand'). GGG's own "
                    "top-tier-base tag is read from the mod database and needs no "
                    "list; this is for bases the market wants and the game files do "
                    "not mark."
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

    def sought_bases(self) -> frozenset[str]:
        """The opinion list, plus whatever the player added.

        Deliberately *not* named ``allowlist`` any more. The old one mixed a factual
        claim (this base is the best of its class) with a market one (people buy this
        blank), and only the second is left here — GGG's own ``top_tier_base_item_type``
        tag answers the first, from data, for 187 bases.
        """
        extra = self._setting("extra_sought_after_bases", [])
        if not isinstance(extra, list):
            return SOUGHT_AFTER_BASES
        return SOUGHT_AFTER_BASES | {str(name) for name in extra if str(name).strip()}

    def widen(self) -> float:
        return float(self._setting("check_widen", DEFAULT_WIDEN))

    def gate(
        self, item: NormalizedItem, *, strictness: Strictness | None = None
    ) -> GateResult:
        return evaluate(
            item,
            strictness=strictness or self.strictness(),
            moddb=self._moddb,
            sought_bases=self.sought_bases(),
        )

    def highlight(
        self, item: NormalizedItem, *, strictness: Strictness | None = None
    ) -> ItemHighlight:
        """The proposal for one item — free, local, and claiming no number.

        `moddb` is read **once** and the reading is handed to both the highlighter and
        the checkbox list, so a mod is never attributed twice and the two can never
        disagree about what the database said.
        """
        report = report_for(item, self._moddb)
        gate = evaluate(
            item,
            strictness=strictness or self.strictness(),
            moddb=self._moddb,
            sought_bases=self.sought_bases(),
            report=report,
        )
        return build_highlight(item, gate, report, moddb=self._moddb)

    async def appraise(
        self,
        items: Sequence[NormalizedItem],
        *,
        strictness: Strictness | None = None,
        threshold_chaos: float | None = None,
        league: str | None = None,
        override: str | None = None,
    ) -> BagAppraisal:
        """One account request, zero trade requests. See the module docstring."""
        rows = list(items)
        level = strictness or self.strictness()
        keep = self.threshold() if threshold_chaos is None else float(threshold_chaos)
        valued = await self._require_prices().value_all(rows, league=league, override=override)
        gates = [self.gate(item, strictness=level) for item in rows]
        return appraise_bag(
            rows,
            valued,
            gates,
            keep_chaos=keep,
            check_chaos=min(self.check_floor(), keep),
            strictness=level,
        )

    async def price_check(
        self,
        item: NormalizedItem,
        selection: Selection | None = None,
        *,
        league: str | None = None,
        override: str | None = None,
        sample: int = 0,
    ) -> PriceCheck:
        """Run the player's query. The only method in this module that spends.

        The query is built from :meth:`Selection.spec` and from nothing else — not
        from the gate, which has no opinion to contribute here, and not from the item,
        whose every mod ANDed together is the search that matched zero listings twice.

        A selection that asks nothing is refused rather than widened. "Price this item
        with no filters" is a base-type search, and the price of the cheapest junk
        sharing a base is the confidently wrong number this phase exists to stop.
        """
        prices = self._require_prices()
        proposal = self.highlight(item)
        chosen = selection or proposal.selection(widen=self.widen())
        if not chosen.asks_anything:
            raise AppraisalError(
                "nothing was selected: tick at least one mod, or ask for an open "
                "prefix or suffix. A search with no filters prices the cheapest item "
                "sharing this base, which is not this item's price."
            )
        choice = prices.league_choice(league, explicit=override)
        before = prices.trade_requests
        try:
            quote = await prices.quote(
                item,
                league=override or league,
                sample=sample,
                spec=chosen.spec(proposal),
            )
        except TradeUnavailable as exc:
            return PriceCheck(
                highlight=proposal,
                selection=chosen,
                league=choice.league,
                quote=TradeQuote(
                    None, considered=0, online=0, total=0, attempts=0, unavailable=str(exc)
                ),
                spent=max(0, prices.trade_requests - before),
            )
        return PriceCheck(
            highlight=proposal,
            selection=chosen,
            league=choice.league,
            quote=quote,
            spent=max(0, prices.trade_requests - before),
        )

    # ``PriceCheck.divine_rate`` is deliberately left unset here. `PricesApi`
    # publishes no divine accessor, and inventing one for a second figure under the
    # first would mean either a fetch inside a method that has already spent its
    # requests, or a rate read from tables that may belong to another league. The bag
    # payload already carries the rate; a surface converts with the one it is showing.

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
        item = await self._find(uid, character)
        return self.gate(item, strictness=_parse_strictness(strictness)).to_json()

    async def highlight_json(
        self, uid: str, character: str | None = None, strictness: str | None = None
    ) -> dict[str, Any]:
        """The checkbox list for one item of the current bag. Costs nothing."""
        item = await self._find(uid, character)
        return self.highlight(item, strictness=_parse_strictness(strictness)).to_json()

    async def price_check_json(
        self,
        uid: str,
        mods: list[int] | None = None,
        open_prefixes: int | None = None,
        open_suffixes: int | None = None,
        character: str | None = None,
        league: str | None = None,
    ) -> dict[str, Any]:
        """Run the player's query for one item of the current bag.

        Takes a **uid and a list of indexes**, never an item and never mod text, so
        the frontend cannot submit an item it invented or a filter the panel never
        showed. ``mods`` of ``None`` means "the pre-ticked set", which is what the
        button does before anything is unticked.
        """
        item = await self._find(uid, character)
        proposal = self.highlight(item)
        selection = proposal.selection(
            mods,
            open_prefixes=open_prefixes,
            open_suffixes=open_suffixes,
            widen=self.widen(),
        )
        result = await self.price_check(item, selection, league=league)
        return result.to_json()

    async def settings_json(self) -> dict[str, Any]:
        return {
            "keep_threshold_chaos": self.threshold(),
            "check_threshold_chaos": self.check_floor(),
            "strictness": self.strictness().value,
            "check_widen": self.widen(),
            "sought_after_bases": sorted(self.sought_bases()),
            "moddb": self._describe_db(),
        }

    async def _find(self, uid: str, character: str | None) -> NormalizedItem:
        bag = await self._require_poeapi().get_items(character)
        for item in bag.items:
            if item.uid == uid:
                return item
        raise AppraisalError(f"no item {uid!r} in the current bag")

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
