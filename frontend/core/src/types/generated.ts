/**
 * GENERATED FILE — do not edit.
 *
 * Emitted from the pydantic models' JSON Schema by `scripts/generate_types.py`.
 * `python3 scripts/generate_types.py` rewrites it; `--check` fails when it is
 * stale, which is what `pnpm run types:check` and `tests/test_generated_types.py`
 * run. The Python and TypeScript item models are not allowed to drift, and this
 * file is the mechanism rather than the promise.
 *
 * Sources:
 *   modules/poeapi/backend/models.py   the normalized item model (SPEC §4.5)
 *   transports/wire.py                 the appraisal/prices payloads and envelopes
 */

/**
 * The boundary (SPEC §4.5). Pricing consumes this, never raw API JSON.
 */
export interface NormalizedItem {
  uid: string
  name: string
  base_type: string
  category: string
  subcategory?: string | null
  rarity?: Rarity
  ilvl?: number
  stack_size?: number
  max_stack_size?: number | null
  map_tier?: number | null
  grid?: Grid
  sockets?: Sockets
  corrupted?: boolean
  fractured?: boolean
  synthesised?: boolean
  identified?: boolean
  influences?: string[]
  mods?: Mods
  note?: string | null
  location: Location
  icon?: string | null
}

/**
 * A bag, an equipment set, or one stash tab, normalized.
 */
export interface ItemSet {
  items?: NormalizedItem[]
  source: Source
  character?: string | null
  league?: string | null
  tab_index?: number | null
  tab_name?: string | null
  meta: Meta
}

export interface CharacterList {
  characters?: Character[]
  meta: Meta
}

/**
 * One resolved unit price. Mirrors ``prices.api.Price.to_json``. ``source`` is
 * the whole point of this model reaching the screen: a poe.ninja line, a GGG
 * bulk-exchange median, a trade search and the player's own ``~price`` note
 * are four different kinds of claim, and a surface that prints only the number
 * has thrown away which one it is showing.
 */
export interface PricePayload {
  chaos: number
  source: "note" | "bulk" | "exchange" | "trade" | "unpriceable"
  category: string | null
  detail: string | null
  listing_count: number | null
  sample_size: number | null
  as_of: string | null
}

/**
 * Mirrors ``prices.api.Valuation.to_json``.
 */
export interface ValuationPayload {
  uid: string
  name: string
  base_type: string
  category: string
  stack_size: number
  unpriceable: boolean
  pricing: boolean
  no_listings: boolean
  tier3: "none" | "pending" | "no_listings" | "failed" | "priced"
  source: "note" | "bulk" | "exchange" | "trade" | "unpriceable"
  total_chaos: number
  price: PricePayload | null
  note_price: PricePayload | null
  market: PricePayload | null
  overpriced_by: number | null
  reason: string | null
}

/**
 * One falsifiable reason the tier-2 gate had an opinion.
 */
export interface GateSignalPayload {
  name: string
  detail: string
  hard: boolean
  mods: string[]
  value: number | null
}

export interface GatePayload {
  passed: boolean
  considered: boolean
  strictness: "generous" | "strict"
  signals: GateSignalPayload[]
}

/**
 * Where an item sits in its container, so the grid can be a map.
 */
export interface SlotPayload {
  x: number
  y: number
  w: number
  h: number
}

/**
 * Mirrors ``appraisal.api.ItemVerdict.to_json``.
 */
export interface ItemVerdictPayload {
  uid: string
  name: string
  base_type: string
  category: string
  rarity: string
  slot: SlotPayload | null
  verdict: "keep" | "check" | "trash" | "unpriceable" | "not_loot"
  stack_size: number
  total_chaos: number
  unpriceable: boolean
  pricing: boolean
  no_listings: boolean
  tier3: "none" | "pending" | "no_listings" | "failed" | "priced"
  highlighted: boolean
  unchecked: boolean
  reason: string
  gate: GatePayload
  valuation: ValuationPayload
}

/**
 * Every state, always present, zeroes included.
 */
export interface VerdictCounts {
  keep: number
  check: number
  trash: number
  unpriceable: number
  not_loot: number
}

export interface TableStatusPayload {
  league: string | null
  loaded: number
  requested: number
  oldest: string | null
  newest: string | null
  stale: boolean
  note: string | null
  discovery: string | null
}

export interface LeagueChoicePayload {
  league: string
  source: "argument" | "setting" | "character"
  overridden: boolean
}

/**
 * Mirrors ``appraisal.api.BagAppraisal.to_json`` plus the two keys
 * ``appraise_bag_json`` adds (``character``, ``stale``).
 */
export interface BagAppraisalPayload {
  league: string
  league_source: "argument" | "setting" | "character" | null
  league_overridden: boolean | null
  strictness: "generous" | "strict"
  threshold_chaos: number
  items: ItemVerdictPayload[]
  counts: VerdictCounts
  total_chaos: number
  total_divine: number | null
  divine_rate: number | null
  unpriceable_count: number
  unpriceable_stack: number
  highlighted_count: number
  unchecked_count: number
  pricing_count: number
  no_listings_count: number
  total_is_floor: boolean
  lookups: number
  trade_requests: number
  table: TableStatusPayload | null
  character?: string | null
  stale?: boolean
}

/**
 * One line of an item as a tickable row. Mirrors ``appraisal.api.ModOption``.
 * ``tier_label`` is the only thing a surface should print, and it is
 * :meth:`ModMatch.describe`'s own wording or the word ``unknown``. ``tier`` is
 * populated only where `moddb` committed to a number; a screen that
 * reconstructed a label from ``tier``/``tiers`` would eventually print one the
 * database refused.
 */
export interface ModOptionPayload {
  index: number
  text: string
  origin: string
  affix: "prefix" | "suffix" | null
  tier: number | null
  tiers: number | null
  tier_label: string
  attribution: "exact" | "group" | "ambiguous" | "unknown"
  top_tier: boolean
  value: number | null
  ceiling: number | null
  influences: string[]
  preticked: boolean
  tradeable: boolean
  suggested_minimum: number | null
}

/**
 * The checkbox list, and **no price at all**. Mirrors ``ItemHighlight``.
 */
export interface ItemHighlightPayload {
  uid: string
  name: string
  base_type: string
  rarity: string
  ilvl: number
  highlighted: boolean
  gate: GatePayload
  mods: ModOptionPayload[]
  preticked: number[]
  open_prefixes: number
  open_suffixes: number
  max_prefixes: number
  max_suffixes: number
  counts_are_certain: boolean
  top_affix_level: number | null
  note: string
}

/**
 * What the player ticked — indexes, never mod text.
 */
export interface SelectionPayload {
  uid: string
  mods: number[]
  open_prefixes: number | null
  open_suffixes: number | null
  widen: number
}

/**
 * Mirrors ``prices.api.TradeQuote.to_json``.
 */
export interface TradeQuotePayload {
  chaos: number | null
  considered: number
  online: number
  total: number
  listings: number[]
  query_url: string | null
  query: string
  attempts: number
  unavailable: string | null
}

/**
 * The answer to a manual check. Mirrors ``appraisal.api.PriceCheck``.
 * ``comparables`` sits beside ``chaos`` rather than behind a detail pane,
 * because a median over one listing is one stranger's asking price and the
 * first live appraisal printed exactly that with nothing to say so.
 */
export interface PriceCheckPayload {
  uid: string
  name: string
  league: string
  chaos: number | null
  divine: number | null
  priced: boolean
  thin: boolean
  comparables: number
  reason: string
  spent: number
  selection: SelectionPayload
  quote: TradeQuotePayload | null
}

/**
 * How old the answer on screen is, and whether the tool can get a fresher one.
 * Six states, and none of them is allowed to be a euphemism for another: *
 * ``fresh`` — fetched now. * ``stale`` — this is cache; the fetch did not
 * happen. * ``syncing`` — a fetch is in flight. The grid dims; it never
 * blanks. * ``unchanged`` — a fetch happened and the bag is byte-identical to
 * last time. The surface says *"no change since HH:MM"*, not *"refreshed"*,
 * because those are different facts and the second one is the one a player
 * stops trusting. * ``error`` — the fetch failed, with a reason. *
 * ``restricted`` — the rate limiter refused. ``retry_after`` is a live
 * countdown and the refresh control is disabled for its duration, rather than
 * swallowing presses that go nowhere.
 */
export interface SyncMeta {
  state: "fresh" | "stale" | "syncing" | "unchanged" | "error" | "restricted"
  at: string | null
  checked_at: string | null
  detail: string | null
  retry_after: number | null
  can_refresh: boolean
}

/**
 * A failed method call. ``kind`` is the exception class name, which is a
 * stable-enough discriminator for a frontend and does not leak a traceback.
 */
export interface CallError {
  kind: string
  message: string
  retry_after?: number | null
}

export interface CallResponse {
  ok: boolean
  result?: unknown | null
  error?: CallError | null
}

export interface ModuleInfo {
  id: string
  name: string
  kind: "core" | "feature"
  state: string
  requires?: string[]
  reason?: string | null
  methods?: string[]
}

/**
 * What the shell asks for before it mounts anything.
 */
export interface ServerMeta {
  version: string
  profile: "compact" | "full"
  modules?: ModuleInfo[]
  methods?: string[]
}

export interface Character {
  name: string
  league?: string | null
  realm?: string | null
  class_name?: string | null
  level?: number
  experience?: number
  current?: boolean
}

/**
 * Slot-accurate placement, so the 12x5 bag grid can be drawn (SPEC §4.5).
 */
export interface Grid {
  x?: number
  y?: number
  w?: number
  h?: number
}

export interface Location {
  source: Source
  tab_id?: string | null
  tab_name?: string | null
  tab_index?: number | null
  slot?: string | null
}

/**
 * Freshness, carried by every response model.
 */
export interface Meta {
  fetched_at: string
  from_cache?: boolean
  stale?: boolean
  retry_after?: number | null
  note?: string | null
}

/**
 * Mod text by origin. Strings, because that is what the API gives and what a
 * human reads; structured stat ids are a trade-API concern and belong to Phase
 * 3.
 */
export interface Mods {
  implicit?: string[]
  explicit?: string[]
  crafted?: string[]
  enchant?: string[]
  fractured?: string[]
  utility?: string[]
  veiled?: string[]
}

/**
 * A 1:1 reading of GGG's ``frameType``. Deliberately not collapsed to
 * normal/magic/rare/unique. ``frameType`` 5 is not "a normal item that happens
 * to be currency"; flattening it would force every consumer to re-derive the
 * distinction from the base type, which is exactly the string matching the
 * normalized model exists to abolish.
 */
export type Rarity = "normal" | "magic" | "rare" | "unique" | "gem" | "currency" | "divination" | "quest" | "prophecy" | "relic" | "unknown"

export interface Sockets {
  count?: number
  links?: number
  colors?: string[]
}

export type Source = "bag" | "equipment" | "stash"
