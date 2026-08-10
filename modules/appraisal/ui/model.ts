/**
 * Turning `appraisal`'s wire payload into the kit's view models.
 *
 * This file is the module's opinion, which is why it lives in the module. The kit
 * knows what "a row with a verdict and a price" looks like; only appraisal knows
 * that a `check` row with a gate signal and no number is a different question from
 * a `check` row worth six chaos, and that the difference is the most important
 * thing on the screen.
 */

import type {
  BagAppraisalPayload,
  ItemHighlightPayload,
  ItemVerdictPayload,
  ModOptionPayload,
  PriceCheckPayload,
  PricePayload,
} from '@poedex/core'
import type {
  BarModel,
  CheckOptionModel,
  DetailModel,
  GridCellModel,
  ItemRowModel,
  PriceProvenance,
  Rarity,
  RowMark,
  TallyEntry,
  Tone,
  Verdict,
} from '@poedex/ui'
import { PROVENANCE_LABEL, VERDICT_HEADLINE, formatChaos, formatDivine } from '@poedex/ui'

/**
 * The `check` block's two jobs, made into two lanes.
 *
 * SPEC §5.4 defines `check` as *"below the threshold but non-trivial, **or** the
 * tier-2 gate flagged it"*. IMPLEMENTATION-PLAN Phase 4 recorded that this is one
 * colour doing two jobs and asked for it to be split before the panel was drawn.
 * It is split here rather than in the backend because the two lanes are still one
 * verdict — the player's action for both is *look before you vendor* — and moving
 * the split into `Verdict` would mean five states, a fifth colour, and a change to
 * the CLI, the event payload and every test, to express a difference that is about
 * *layout*.
 *
 * The line is drawn on whether a **number** exists:
 *
 * * `priced` — the tool knows what it is worth and it is a small amount. There is
 *   nothing left to find out; the row is a subtotal.
 * * `unknown` — no bulk table will ever price it, the gate found reasons, and a
 *   trade search is what settles it. This is where the tool earns its keep.
 */
export type CheckLane = 'priced' | 'unknown'

export function checkLane(item: ItemVerdictPayload): CheckLane {
  return item.pricing || item.valuation.unpriceable ? 'unknown' : 'priced'
}

/**
 * Did a tier-3 query finish and match nothing?
 *
 * The distinction the first live appraisal did not have. `pricing` promises a
 * number; this says the search ran, broadened, and the league had nothing
 * comparable. Both leave the row without a price, and they are opposite kinds of
 * without: one resolves, the other already has.
 */
export function isTerminalNoPrice(item: ItemVerdictPayload): boolean {
  return item.no_listings || item.valuation.tier3 === 'failed'
}

export function rarityOf(item: { rarity: string }): Rarity {
  return item.rarity as Rarity
}

/** Stack-aware chaos, or `null` when there is genuinely no number. */
export function priceOf(item: ItemVerdictPayload, divineRate: number | null | undefined) {
  const priced = !item.valuation.unpriceable
  const chaos = priced ? item.total_chaos : null
  return {
    chaos,
    provenance: item.valuation.source as PriceProvenance,
    detail: describePrice(item.valuation.price, item),
    divine: chaos !== null && divineRate ? chaos / divineRate : null,
    pricing: item.pricing,
    noListings: isTerminalNoPrice(item),
  }
}

function describePrice(price: PricePayload | null | undefined, item: ItemVerdictPayload) {
  if (item.pricing) return 'a trade query is still outstanding'
  if (item.no_listings) {
    // Terminal, and phrased so nobody waits for it. `item.valuation.reason` carries
    // the query that found nothing, which is what makes the claim checkable.
    const query = item.valuation.reason
    return query
      ? `the trade search found no matching listings — ${query}`
      : 'the trade search found no matching listings'
  }
  if (item.valuation.tier3 === 'failed') {
    return item.valuation.reason
      ? `the trade search could not run — ${item.valuation.reason}`
      : 'the trade search could not run'
  }
  if (!price) return item.valuation.reason ?? null
  const parts = [PROVENANCE_LABEL[price.source] ?? price.source]
  if (item.stack_size > 1) parts.push(`${formatChaos(price.chaos)}c each`)
  if (price.detail) parts.push(price.detail)
  return parts.join(' · ')
}

/**
 * One or two characters for a 22 px cell.
 *
 * Initials of the significant words, so `Divine Orb` is `DO` and
 * `Scarab of Monstrous Lineage` is `SM` — deliberately not the first two letters,
 * which would make every scarab in the bag read `Sc`.
 */
export function glyphFor(name: string): string {
  const words = name
    .replace(/['’]s\b/g, '')
    .split(/[\s-]+/)
    .filter((word) => word.length > 2 && !/^(of|the|and|to)$/i.test(word))
  if (words.length >= 2) return `${words[0]![0]}${words[1]![0]}`.toUpperCase()
  return (words[0] ?? name).slice(0, 2)
}

export function toGridCell(item: ItemVerdictPayload): GridCellModel | null {
  if (!item.slot) return null
  return {
    uid: item.uid,
    glyph: glyphFor(item.name),
    label: `${item.name}${item.stack_size > 1 ? ` ×${item.stack_size}` : ''}`,
    verdict: item.verdict as Verdict,
    rarity: rarityOf(item),
    x: item.slot.x,
    y: item.slot.y,
    w: item.slot.w,
    h: item.slot.h,
    quantity: item.stack_size,
  }
}

export function subtitleOf(item: ItemVerdictPayload): string {
  if (item.base_type && item.base_type !== item.name) return item.base_type
  return item.category
}

export function marksOf(item: ItemVerdictPayload): RowMark[] {
  const marks: RowMark[] = []
  if (item.verdict === 'not_loot') {
    // A mark rather than only a reason, because `compact` has no reason column: at
    // 300 px the row would otherwise read `The Mortinomicon Exitio Immortalis · quest
    // · —`, and a dash in a money column is an invitation to ask what the money was.
    marks.push({ id: 'not-loot', label: 'not loot', tone: 'quiet', detail: item.reason })
  }
  if (item.pricing) {
    marks.push({ id: 'pricing', label: 'pricing…', tone: 'accent', detail: 'tier 3 outstanding' })
  } else if (item.no_listings) {
    // A mark, not an absence of one: "we looked and there was nothing" is a fact the
    // player wants, and it is the difference between an item nobody wants and an
    // item nobody has listed.
    marks.push({
      id: 'no-listings',
      label: 'no listings',
      tone: 'quiet',
      detail: item.valuation.reason
        ? `the trade search found nothing comparable — ${item.valuation.reason}`
        : 'the trade search found nothing comparable',
    })
  } else if (item.valuation.tier3 === 'failed') {
    marks.push({
      id: 'tier3-failed',
      label: 'search failed',
      tone: 'warn',
      detail: item.valuation.reason ?? 'the trade search could not run',
    })
  }
  for (const signal of item.gate.signals) {
    marks.push({
      id: `gate-${signal.name}`,
      label: signal.name,
      tone: signal.hard ? 'warn' : 'quiet',
      detail: signal.detail,
    })
  }
  const { note_price: note, market, overpriced_by: ratio } = item.valuation
  if (note && market && ratio) {
    marks.push({
      id: 'note',
      label: ratio >= 1 ? `your price ${ratio.toFixed(1)}×` : `under market ${ratio.toFixed(1)}×`,
      tone: ratio >= 1.5 ? 'warn' : 'good',
      detail: `your ~price note is ${formatChaos(note.chaos)}c; poe.ninja says ${formatChaos(
        market.chaos,
      )}c`,
    })
  }
  return marks
}

export function toRow(
  item: ItemVerdictPayload,
  divineRate: number | null | undefined,
): ItemRowModel {
  return {
    uid: item.uid,
    name: item.name,
    rarity: rarityOf(item),
    verdict: item.verdict as Verdict,
    subtitle: subtitleOf(item),
    quantity: item.stack_size,
    price: priceOf(item, divineRate),
    reason: item.reason,
    marks: marksOf(item),
  }
}

export function toDetail(
  item: ItemVerdictPayload,
  divineRate: number | null | undefined,
): DetailModel {
  const { note_price: note, market } = item.valuation
  const comparables: BarModel[] = []
  if (note && market) {
    // The one comparison the payload can actually support today: the player's own
    // asking price against the index (SPEC §6.3). Tier-3 listing spreads are not on
    // the wire, so the detail pane does not pretend to draw them.
    comparables.push(
      { id: 'market', label: 'poe.ninja', value: market.chaos, display: `${formatChaos(market.chaos)}c`, tone: 'quiet' },
      { id: 'note', label: 'your note', value: note.chaos, display: `${formatChaos(note.chaos)}c`, tone: 'accent' },
    )
  }
  return {
    uid: item.uid,
    name: item.name,
    rarity: rarityOf(item),
    verdict: item.verdict as Verdict,
    subtitle: subtitleOf(item),
    quantity: item.stack_size,
    price: priceOf(item, divineRate),
    reason: item.reason,
    location: item.slot ? `bag ${item.slot.x + 1},${item.slot.y + 1}` : undefined,
    comparables,
    gate: {
      passed: item.gate.passed,
      considered: item.gate.considered,
      signals: item.gate.signals.map((signal) => ({
        id: signal.name,
        label: signal.name,
        tone: signal.hard ? 'warn' : 'quiet',
        detail: signal.detail,
      })),
    },
  }
}

export function tallyOf(bag: BagAppraisalPayload): TallyEntry[] {
  const counts = bag.counts
  return [
    { id: 'keep', label: 'keep', count: counts.keep, verdict: 'keep' },
    { id: 'check', label: 'check', count: counts.check, verdict: 'check' },
    // Always present, zeroes included: a tally that drops `unpriceable` when a bag
    // happens to have none teaches the player that the state does not exist.
    { id: 'unpriceable', label: 'unpriced', count: counts.unpriceable, verdict: 'unpriceable' },
    { id: 'trash', label: 'trash', count: counts.trash, verdict: 'trash' },
    { id: 'not_loot', label: 'not loot', count: counts.not_loot, verdict: 'not_loot' },
  ]
}

export interface Block {
  verdict: Verdict
  lane?: CheckLane
  items: ItemVerdictPayload[]
  /** Chaos across the rows that have a price. Unpriceable rows contribute nothing. */
  subtotal: number
  units: number
}

/**
 * The screen's blocks, in the order the player acts on them, with `check` split.
 *
 * `items` arrives already ranked by the backend (`BagAppraisal.ranked()`), so this
 * partitions and never re-sorts: two runs of the same bag must not disagree, and
 * the backend's tie-break — gate hits first, then hard-signal count, then value —
 * is a better order than anything derivable here.
 */
export function blocksOf(bag: BagAppraisalPayload): Block[] {
  const rows = bag.items
  const pick = (predicate: (item: ItemVerdictPayload) => boolean) => rows.filter(predicate)
  const build = (verdict: Verdict, items: ItemVerdictPayload[], lane?: CheckLane): Block => ({
    verdict,
    lane,
    items,
    subtotal: items.reduce(
      (sum, item) => sum + (item.valuation.unpriceable ? 0 : item.total_chaos),
      0,
    ),
    units: items.reduce((sum, item) => sum + item.stack_size, 0),
  })

  return [
    build('keep', pick((item) => item.verdict === 'keep')),
    build(
      'check',
      pick((item) => item.verdict === 'check' && checkLane(item) === 'unknown'),
      'unknown',
    ),
    build(
      'check',
      pick((item) => item.verdict === 'check' && checkLane(item) === 'priced'),
      'priced',
    ),
    build('unpriceable', pick((item) => item.verdict === 'unpriceable')),
    build('trash', pick((item) => item.verdict === 'trash')),
    build('not_loot', pick((item) => item.verdict === 'not_loot')),
  ]
}

export interface BlockCopy {
  title: string
  headline: string
}

export function copyFor(block: Block): BlockCopy {
  if (block.verdict === 'check' && block.lane === 'unknown') {
    return {
      title: 'check · no price yet',
      headline: 'the gate flagged these — a trade search is what settles them',
    }
  }
  if (block.verdict === 'check') {
    return {
      title: 'check · priced',
      headline: 'below the keep threshold, above trivial. A number, just a small one',
    }
  }
  if (block.verdict === 'not_loot') {
    return { title: 'not loot', headline: VERDICT_HEADLINE.not_loot }
  }
  return { title: block.verdict, headline: VERDICT_HEADLINE[block.verdict] }
}

/** `1,824c` and `8.53 div`, with the floor marker handled by `Stat.prefix`. */
export function totalsOf(bag: BagAppraisalPayload) {
  return {
    chaos: formatChaos(bag.total_chaos),
    divine: bag.total_divine !== null && bag.total_divine !== undefined
      ? `${formatDivine(bag.total_divine)} div`
      : null,
    isFloor: bag.total_is_floor || bag.unpriceable_count > 0,
  }
}

/**
 * What the total leaves out, in one sentence — or `null` when it leaves out nothing.
 *
 * Three different holes, and none of them may be merged into another:
 *
 * * an `unpriceable` row is money the tool cannot see at all;
 * * a `pricing…` row is money it is in the middle of looking up — this one resolves;
 * * a `no listings` row is money it **finished** looking up and could not find a
 *   comparable for. It does not resolve, and it is the one this sentence used to get
 *   wrong: the first live appraisal said "2 items still pricing" about two searches
 *   that had already come back empty.
 *
 * Only the middle one makes the figure a floor in the `≥` sense, because only it is
 * going to move. The others make it incomplete, which the last clause says.
 */
export function floorNote(bag: BagAppraisalPayload): string | null {
  const parts: string[] = []
  const plural = (n: number) => (n === 1 ? '' : 's')
  if (bag.unpriceable_count > 0) {
    parts.push(
      `excludes ${bag.unpriceable_count} unpriceable row${plural(bag.unpriceable_count)}` +
        ` (${bag.unpriceable_stack.toLocaleString('en-US')} units) — not in the price index`,
    )
  }
  if (bag.pricing_count > 0) {
    parts.push(
      `${bag.pricing_count} item${plural(bag.pricing_count)} still pricing; their value is not in this figure`,
    )
  }
  if (bag.no_listings_count > 0) {
    parts.push(
      `${bag.no_listings_count} item${plural(bag.no_listings_count)} searched with no matching listings —` +
        ' unknown, not zero, and not still running',
    )
  }
  if (parts.length === 0) return null
  return `${parts.join('; ')}. This is a floor, not a value.`
}

/* -- Phase 9: the checkbox list ---------------------------------------------- */

/**
 * One mod line as a tickable row.
 *
 * `badge` is `tier_label` **verbatim** — `T4 of 10`, or the word `unknown`. It is
 * never recomposed from `tier`/`tiers` here: `moddb` withholds a tier for roughly
 * one affix line in five, and any formatter that built the string itself would have
 * to invent something for those. The refusal travels intact from the database to the
 * pixel.
 */
export function optionsOf(highlight: ItemHighlightPayload): CheckOptionModel[] {
  return highlight.mods.map((mod) => ({
    id: String(mod.index),
    label: mod.text,
    badge: mod.tier_label,
    meta: metaOf(mod),
    tone: toneOf(mod),
  }))
}

function metaOf(mod: ModOptionPayload): string | undefined {
  const parts: string[] = []
  // Annotated, never disabled. `moddb`'s offline bridge is a trimmed artifact with
  // real holes (`98% increased Energy Shield` is one), and greying out a filter that
  // would have worked narrows what the player is allowed to ask. The live document
  // decides, and the query description reports whatever it could not resolve.
  if (!mod.tradeable) parts.push('no offline trade id')
  if (mod.affix) parts.push(mod.affix)
  if (mod.origin !== 'explicit') parts.push(mod.origin)
  if (mod.value !== null && mod.ceiling !== null) {
    // "130 of 144" is what turns "is 130 life good?" into a question with an answer,
    // and it is a per-base number: the same roll is 130 of 189 on a body armour.
    parts.push(`${mod.value} of ${mod.ceiling}`)
  }
  if (mod.influences.length > 0) parts.push(mod.influences.join('/'))
  if (mod.suggested_minimum !== null) parts.push(`searches ≥ ${mod.suggested_minimum}`)
  return parts.length > 0 ? parts.join(' · ') : undefined
}

/**
 * The colour of a tier badge.
 *
 * `unknown` is **quiet**, not a warning. It is the normal state of about a fifth of
 * every list, and an alarm colour on a fifth of every list is an alarm colour the
 * eye stops seeing.
 */
function toneOf(mod: ModOptionPayload): Tone {
  if (mod.influences.length > 0) return 'accent'
  if (mod.top_tier) return 'good'
  if (mod.tier === null) return 'quiet'
  return 'neutral'
}

/** The pre-ticked ids: top-tier rolls and influence mods, as the backend decided. */
export function ticksOf(highlight: ItemHighlightPayload): string[] {
  return highlight.preticked.map(String)
}

/** The big number, or a dash. Never a zero — an unchecked item is not worthless. */
export function priceLine(result: PriceCheckPayload): string {
  return result.chaos === null ? '—' : formatChaos(result.chaos)
}

/**
 * The sentence under the number, and it is not optional.
 *
 * `10.0c` over one listing and `10.0c` over 438 are different claims wearing the
 * same number, and the first live appraisal printed the first as though it were the
 * second. The backend already writes the honest sentence; this only appends what it
 * cost.
 */
export function checkNote(result: PriceCheckPayload): string {
  const spent = `${result.spent} trade request${result.spent === 1 ? '' : 's'}`
  return `${result.reason} · ${spent}`
}
