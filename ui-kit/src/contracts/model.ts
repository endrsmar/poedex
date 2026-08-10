/**
 * View models the kit's data primitives speak in.
 *
 * These are *not* the wire types. A module maps `ItemVerdictPayload` onto
 * `ItemRowModel` itself, and that mapping is the module's opinion about what
 * matters — which is exactly where an opinion belongs. The kit knows about
 * "a row with a verdict, a name, a quantity and a price"; it does not know what
 * a tier-2 gate is.
 */

/** SPEC §5.4. Four states, and the fourth is not a worse third. */
export type Verdict = 'keep' | 'check' | 'trash' | 'unpriceable'

/** PoE's own rarity vocabulary, carried through so a name can be coloured. */
export type Rarity =
  | 'normal'
  | 'magic'
  | 'rare'
  | 'unique'
  | 'gem'
  | 'currency'
  | 'divination'
  | 'quest'
  | 'prophecy'
  | 'relic'
  | 'unknown'

export type Tone = 'neutral' | 'good' | 'warn' | 'bad' | 'quiet' | 'accent'

/**
 * A price the surface may show, with the claim it rests on.
 *
 * `provenance` is required. Phase 4b's finding was that a number's *kind* is part
 * of the answer — poe.ninja's index, GGG's bulk exchange, a live trade search and
 * the player's own `~price` note are four different assertions about the world,
 * and one of them is not even a market claim. A `PriceModel` that could omit it
 * would let a screen quietly print all four the same way.
 */
export interface PriceModel {
  /** Already stack-multiplied where that is what the caller means to show. */
  chaos: number | null
  provenance: PriceProvenance
  /** How the line was identified: matched variant, links, the note's own text. */
  detail?: string | null
  /** Divine equivalent, when a rate is known and the figure is large enough. */
  divine?: number | null
  /** `true` while a tier-3 query is outstanding: show `⋯`, never `0c`. */
  pricing?: boolean
}

export type PriceProvenance = 'note' | 'bulk' | 'exchange' | 'trade' | 'unpriceable'

/** One cell of the bag grid. `x`/`y` are the game's own slot coordinates. */
export interface GridCellModel {
  uid: string
  /** 1–3 characters. `compact` has 22 px and shows one; `full` shows more. */
  glyph: string
  /** Read out to assistive tech and shown on hover; never relied on for layout. */
  label: string
  verdict: Verdict
  rarity: Rarity
  x: number
  y: number
  w?: number
  h?: number
  quantity?: number
}

/** One row of a verdict list. */
export interface ItemRowModel {
  uid: string
  name: string
  rarity: Rarity
  verdict: Verdict
  /** `Onyx Amulet · i86`, `Scarab`, `7 items · pricing…` — the mockup's `.sub`. */
  subtitle?: string
  quantity?: number
  price?: PriceModel | null
  /** Why this verdict. A falsifiable claim the player is invited to disagree with. */
  reason?: string
  /** Rendered as a set of small labels: gate signals, `override`, `note`. */
  marks?: RowMark[]
}

export interface RowMark {
  id: string
  label: string
  tone?: Tone
  /** Long form for a tooltip / detail line. `compact` may drop this entirely. */
  detail?: string
}

/** One bar of a `ValueBar`. */
export interface BarModel {
  id: string
  label: string
  value: number
  tone?: Tone
  /** Shown at the right of the bar. Formatted by the caller; right-aligned tnum. */
  display?: string
}

export interface TallyEntry {
  id: string
  label: string
  count: number
  verdict?: Verdict
  tone?: Tone
  /** `true` renders it present but recessive — a zero that still holds its slot. */
  muted?: boolean
}

/** Fields a `Detail` may be asked to show, in the order it shows them. */
export type DetailField =
  | 'name'
  | 'value'
  | 'provenance'
  | 'stack'
  | 'location'
  | 'reason'
  | 'gate'
  | 'mods'
  | 'comparables'

export interface DetailModel {
  uid: string
  name: string
  rarity: Rarity
  verdict: Verdict
  subtitle?: string
  quantity?: number
  price?: PriceModel | null
  reason?: string
  location?: string
  /** Implicit/explicit/crafted lines, already grouped and ordered by the module. */
  mods?: { group: string; lines: string[] }[]
  /** The listings behind a tier-3 number. Empty for every bulk-priced row. */
  comparables?: BarModel[]
  gate?: { passed: boolean; considered: boolean; signals: RowMark[] }
}

/** The six honest sync states. Mirrors `transports/wire.py:SyncState`. */
export type SyncState = 'fresh' | 'stale' | 'syncing' | 'unchanged' | 'error' | 'restricted'

export interface SyncStatus {
  state: SyncState
  /** ISO timestamp of the data on screen — not of this status. */
  at?: string | null
  detail?: string | null
  /**
   * Seconds until the limiter will accept a request again. Counted down by the
   * surface, and the reason `StaleBanner` disables its own control rather than
   * letting a press disappear.
   */
  retryAfter?: number | null
}
