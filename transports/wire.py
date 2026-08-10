"""The wire schema: pydantic models for everything that crosses to a frontend.

IMPLEMENTATION-PLAN §3 says *"Pydantic models are the single source of truth for
types; a build step generates TS from their JSON Schema and CI fails if it is
stale."* Two of this project's three model families were already pydantic —
`poeapi`'s normalized item and its friends. The third was not: `prices` and
`appraisal` deliberately use plain classes with a ``to_json()`` method, because
they are constructed in tight loops and a pydantic model per valuation is a real
cost for no gain inside the backend (`Price`'s own docstring says so).

That left a hole. ``BagAppraisal.to_json()`` is a hand-written dict, and the bag
screen is written against its keys; nothing connected the two. So the wire shape is
declared **here**, in the transport layer, as pydantic:

* :mod:`scripts.generate_types` turns these schemas into
  ``frontend/core/src/types/generated.ts``, and ``--check`` fails when the checked-in
  file no longer matches. That is the drift check the plan asks for.
* ``tests/test_transport_wire.py`` validates the output of the *real*
  ``to_json()`` methods against these models. That is the other half: a schema
  nothing validates against is a wish, not a contract. If someone renames
  ``total_is_floor`` in `appraisal`, that test fails before the frontend does.

The models are `extra="forbid"` on purpose. A key that appears in ``to_json()`` and
not here is exactly the drift this file exists to catch, and silently tolerating it
would make the TS types quietly incomplete instead of loudly wrong.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "WIRE_MODELS",
    "BagAppraisalPayload",
    "CallError",
    "CallResponse",
    "CrawlPlanPayload",
    "GatePayload",
    "GateSignalPayload",
    "ItemHighlightPayload",
    "ItemVerdictPayload",
    "LeagueChoicePayload",
    "ModOptionPayload",
    "ModuleInfo",
    "PriceCheckPayload",
    "PricePayload",
    "SelectionPayload",
    "ServerMeta",
    "SlotPayload",
    "StashDigestPayload",
    "StashTabPayload",
    "SyncMeta",
    "TabAppraisalPayload",
    "TableStatusPayload",
    "TradeQuotePayload",
    "ValuationPayload",
    "VerdictCounts",
]

Verdict = Literal["keep", "check", "trash", "unpriceable", "not_loot"]
# `not_loot` is a quest item, an MTX effect or a hideout decoration — a row that
# is not a loot decision at all. It is a verdict rather than a filtered-out row
# because the bag grid needs a verdict for every cell it draws.
# The last member is spelled `unpriceable`, not `none`: `PriceSource.NONE` has
# value "unpriceable" in `prices/api.py`, and this list is what a screen switches on.
# Guessing it was the enum *member* name is exactly the drift this file catches.
PriceSourceName = Literal["note", "bulk", "exchange", "trade", "unpriceable"]
Tier3Name = Literal["none", "pending", "no_listings", "failed", "priced"]
# What tier 3 did. `pending` is the only one that may render as "pricing…";
# `no_listings` and `failed` are finished answers that used to wear its clothes.
StrictnessName = Literal["generous", "strict"]
LeagueSourceName = Literal["argument", "setting", "character"]

# The six honest sync states (Phase 5's brief). Declared here rather than in the
# frontend so that both sides — and, in Phase 7, a second transport — share one
# vocabulary instead of two lists that agree today. The frontend store is what
# *computes* the state, because `unchanged` is a comparison between two answers and
# only the side holding the previous answer can make it.
SyncState = Literal["fresh", "stale", "syncing", "unchanged", "error", "restricted"]


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PricePayload(Wire):
    """One resolved unit price. Mirrors ``prices.api.Price.to_json``.

    ``source`` is the whole point of this model reaching the screen: a poe.ninja
    line, a GGG bulk-exchange median, a trade search and the player's own ``~price``
    note are four different kinds of claim, and a surface that prints only the
    number has thrown away which one it is showing.
    """

    chaos: float
    source: PriceSourceName
    category: str | None
    detail: str | None
    listing_count: int | None
    sample_size: int | None
    as_of: str | None


class ValuationPayload(Wire):
    """Mirrors ``prices.api.Valuation.to_json``."""

    uid: str
    name: str
    base_type: str
    category: str
    stack_size: int
    unpriceable: bool
    pricing: bool
    no_listings: bool
    tier3: Tier3Name
    source: PriceSourceName
    total_chaos: float
    price: PricePayload | None
    note_price: PricePayload | None
    market: PricePayload | None
    overpriced_by: float | None
    reason: str | None


class GateSignalPayload(Wire):
    """One falsifiable reason the tier-2 gate had an opinion."""

    name: str
    detail: str
    hard: bool
    mods: list[str]
    value: float | None


class GatePayload(Wire):
    passed: bool
    considered: bool
    strictness: StrictnessName
    signals: list[GateSignalPayload]


class SlotPayload(Wire):
    """Where an item sits in its container, so the grid can be a map."""

    x: int
    y: int
    w: int
    h: int


class ItemVerdictPayload(Wire):
    """Mirrors ``appraisal.api.ItemVerdict.to_json``."""

    uid: str
    name: str
    base_type: str
    category: str
    rarity: str
    slot: SlotPayload | None
    verdict: Verdict
    stack_size: int
    total_chaos: float
    unpriceable: bool
    pricing: bool
    no_listings: bool
    tier3: Tier3Name
    highlighted: bool
    """Worth *asking* about. Renamed from ``escalate`` in Phase 9, which is the pivot
    in one word: nothing acts on this but the player."""

    unchecked: bool
    """Highlighted, unpriced, and nobody has asked the market yet. The row the bag
    total is missing, and the reason it can be a floor with no query outstanding."""

    reason: str
    gate: GatePayload
    valuation: ValuationPayload


class VerdictCounts(Wire):
    """Every state, always present, zeroes included."""

    keep: int
    check: int
    trash: int
    unpriceable: int
    not_loot: int


class TableStatusPayload(Wire):
    league: str | None
    loaded: int
    requested: int
    oldest: str | None
    newest: str | None
    stale: bool
    note: str | None
    discovery: str | None


class LeagueChoicePayload(Wire):
    league: str
    source: LeagueSourceName
    overridden: bool


class BagAppraisalPayload(Wire):
    """Mirrors ``appraisal.api.BagAppraisal.to_json`` plus the two keys
    ``appraise_bag_json`` adds (``character``, ``stale``)."""

    league: str
    league_source: LeagueSourceName | None
    league_overridden: bool | None
    strictness: StrictnessName
    threshold_chaos: float
    items: list[ItemVerdictPayload]
    counts: VerdictCounts
    total_chaos: float
    total_divine: float | None
    divine_rate: float | None
    unpriceable_count: int
    unpriceable_stack: int
    highlighted_count: int
    unchecked_count: int
    pricing_count: int
    no_listings_count: int
    total_is_floor: bool
    lookups: int
    trade_requests: int
    table: TableStatusPayload | None
    character: str | None = None
    stale: bool = False


class ModOptionPayload(Wire):
    """One line of an item as a tickable row. Mirrors ``appraisal.api.ModOption``.

    ``tier_label`` is the only thing a surface should print, and it is
    :meth:`ModMatch.describe`'s own wording or the word ``unknown``. ``tier`` is
    populated only where `moddb` committed to a number; a screen that reconstructed a
    label from ``tier``/``tiers`` would eventually print one the database refused.

    ``suggested_minimum`` and ``suggested_maximum`` are exclusive: a row has one bound
    or the other, and which one is ``higher_is_better``. A ``-9 to Total Mana Cost of
    Skills`` row searches ``≤ -7.2``, and a panel that printed ``≥ -7.2`` would be
    describing a filter for strictly worse items in words that read like a promise.
    """

    index: int
    text: str
    origin: str
    affix: Literal["prefix", "suffix"] | None
    tier: int | None
    tiers: int | None
    tier_label: str
    attribution: Literal["exact", "group", "ambiguous", "unknown"]
    top_tier: bool
    value: float | None
    ceiling: float | None
    influences: list[str]
    preticked: bool
    tradeable: bool
    higher_is_better: bool
    suggested_minimum: float | None
    suggested_maximum: float | None


class ItemHighlightPayload(Wire):
    """The checkbox list, and **no price at all**. Mirrors ``ItemHighlight``."""

    uid: str
    name: str
    base_type: str
    rarity: str
    ilvl: int
    highlighted: bool
    gate: GatePayload
    mods: list[ModOptionPayload]
    preticked: list[int]
    open_prefixes: int
    open_suffixes: int
    max_prefixes: int
    max_suffixes: int
    counts_are_certain: bool
    top_affix_level: int | None
    note: str


class SelectionPayload(Wire):
    """What the player ticked — indexes, never mod text."""

    uid: str
    mods: list[int]
    open_prefixes: int | None
    open_suffixes: int | None
    widen: float


class TradeQuotePayload(Wire):
    """Mirrors ``prices.api.TradeQuote.to_json``."""

    chaos: float | None
    considered: int
    online: int
    total: int
    listings: list[float]
    query_url: str | None
    query: str
    attempts: int
    unavailable: str | None


class PriceCheckPayload(Wire):
    """The answer to a manual check. Mirrors ``appraisal.api.PriceCheck``.

    ``comparables`` sits beside ``chaos`` rather than behind a detail pane, because a
    median over one listing is one stranger's asking price and the first live
    appraisal printed exactly that with nothing to say so.
    """

    uid: str
    name: str
    league: str
    chaos: float | None
    divine: float | None
    priced: bool
    thin: bool
    comparables: int
    reason: str
    spent: int
    selection: SelectionPayload
    quote: TradeQuotePayload | None


class CrawlPlanPayload(Wire):
    """What a full stash refresh costs *right now*. Mirrors ``poeapi.CrawlPlan``.

    On the wire because a surface offering a crawl has to be able to say what it
    costs before the press, and because the figure falls as the cache fills — a
    screen that hardcoded "~30 min" would keep saying it after the crawl was done.
    """

    league: str
    total_tabs: int
    cached_tabs: int
    permanent_tabs: int
    unsupported_tabs: int
    requests: int
    seconds: float
    warning: str


class StashTabPayload(Wire):
    """One row of the stash list. Mirrors ``appraisal.TabSummary.to_json``.

    Three fields carry the whole honesty of this screen and none of them may be
    collapsed into a number:

    * ``known`` — has anybody read this tab? ``false`` means its value is *unknown*,
      and ``total_chaos`` is 0 only because there is nothing to add, not because the
      tab is empty. ``item_count`` is ``null`` for the same reason.
    * ``supported`` / ``unsupported_reason`` — a map tab, which this API cannot read
      at all. Rendered as "not supported yet", never as 0c.
    * ``grid`` — whether the tab has a lattice. A currency tab does not, and drawing
      one produces a picture that looks right and is not.
    """

    index: int
    name: str
    type: str
    kind: str
    cols: int | None
    rows: int | None
    grid: bool
    colour: str | None
    hidden: bool
    remove_only: bool
    supported: bool
    unsupported_reason: str | None
    known: bool
    cached: bool
    fetched_at: str | None
    age_seconds: float | None
    stale: bool
    permanent: bool
    """Remove-only: cached once and correct forever. Never offer to refresh it."""

    item_count: int | None
    units: int
    composition: Literal["empty", "bulk", "gear", "mixed"] | None
    total_chaos: float
    highlighted: int
    unpriceable_count: int
    hole: bool


class StashDigestPayload(Wire):
    """Every tab, and an honest total over the ones that have been read."""

    league: str
    strictness: StrictnessName
    tabs: list[StashTabPayload]
    total_chaos: float
    total_divine: float | None
    divine_rate: float | None
    tab_count: int
    known_count: int
    unread_count: int
    unsupported_count: int
    highlighted_count: int
    total_is_floor: bool
    cost: CrawlPlanPayload


class TabAppraisalPayload(BagAppraisalPayload):
    """One tab's contents, judged. A ``BagAppraisal`` plus where it came from.

    Inherits rather than repeats, because that is the claim: a stash tab is the same
    verdicts over items that live somewhere else, not a second verdict model.
    """

    tab: StashTabPayload
    unsupported: str | None = None
    composition: Literal["empty", "bulk", "gear", "mixed"] | None = None


class SyncMeta(Wire):
    """How old the answer on screen is, and whether the tool can get a fresher one.

    Six states, and none of them is allowed to be a euphemism for another:

    * ``fresh`` — fetched now.
    * ``stale`` — this is cache; the fetch did not happen.
    * ``syncing`` — a fetch is in flight. The grid dims; it never blanks.
    * ``unchanged`` — a fetch happened and the bag is byte-identical to last time.
      The surface says *"no change since HH:MM"*, not *"refreshed"*, because those
      are different facts and the second one is the one a player stops trusting.
    * ``error`` — the fetch failed, with a reason.
    * ``restricted`` — the rate limiter refused. ``retry_after`` is a live countdown
      and the refresh control is disabled for its duration, rather than swallowing
      presses that go nowhere.
    """

    state: SyncState
    at: str | None
    """When the data on screen was fetched. Not when this status was computed."""

    checked_at: str | None
    detail: str | None
    retry_after: float | None
    can_refresh: bool


class CallError(Wire):
    """A failed method call. ``kind`` is the exception class name, which is a
    stable-enough discriminator for a frontend and does not leak a traceback."""

    kind: str
    message: str
    retry_after: float | None = None


class CallResponse(Wire):
    ok: bool
    result: object | None = None
    error: CallError | None = None


class ModuleInfo(Wire):
    id: str
    name: str
    kind: Literal["core", "feature"]
    state: str
    requires: list[str] = Field(default_factory=list)
    reason: str | None = None
    methods: list[str] = Field(default_factory=list)


class ServerMeta(Wire):
    """What the shell asks for before it mounts anything."""

    version: str
    profile: Literal["compact", "full"]
    modules: list[ModuleInfo] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)


# The models the TS generator emits, in the order they should appear. Listed
# explicitly rather than discovered, so adding a model to this file is a decision
# to put it on the wire rather than an accident of importing it.
WIRE_MODELS: tuple[type[BaseModel], ...] = (
    PricePayload,
    ValuationPayload,
    GateSignalPayload,
    GatePayload,
    SlotPayload,
    ItemVerdictPayload,
    VerdictCounts,
    TableStatusPayload,
    LeagueChoicePayload,
    BagAppraisalPayload,
    ModOptionPayload,
    ItemHighlightPayload,
    SelectionPayload,
    TradeQuotePayload,
    PriceCheckPayload,
    CrawlPlanPayload,
    StashTabPayload,
    StashDigestPayload,
    TabAppraisalPayload,
    SyncMeta,
    CallError,
    CallResponse,
    ModuleInfo,
    ServerMeta,
)
