/**
 * Verdict vocabulary, in the kit so both profiles say the same words.
 *
 * SPEC §5.4 asks for shape *and* colour, so the grid survives greyscale, a
 * screenshot and a colourblind reader. The glyphs are the CLI's — `cli/appraise.py`
 * chose them to differ in ink density as well as outline — and reusing them means
 * the terminal and the panel are recognisably the same tool rather than two tools
 * that agree.
 */

import type { Verdict } from './contracts/model'

export const VERDICTS: readonly Verdict[] = [
  'keep',
  'check',
  'trash',
  'unpriceable',
  'not_loot',
] as const

/** The order the player acts in: keep, check, unpriceable, trash, not-loot.
 * Unpriceable sits above trash because an unknown is a thing to look at and a trash
 * verdict is a thing to stop looking at. `not_loot` is last because it is the only
 * block that asks for nothing at all. */
export const VERDICT_ORDER: readonly Verdict[] = [
  'keep',
  'check',
  'unpriceable',
  'trash',
  'not_loot',
] as const

export const VERDICT_GLYPH: Record<Verdict, string> = {
  keep: '●',
  check: '◐',
  unpriceable: '?',
  trash: '·',
  not_loot: '◇',
}

export const VERDICT_LABEL: Record<Verdict, string> = {
  keep: 'keep',
  check: 'check',
  unpriceable: 'unpriceable',
  trash: 'trash',
  not_loot: 'not loot',
}

/** The one-line claim each verdict is making. Straight from `cli/appraise.py`. */
export const VERDICT_HEADLINE: Record<Verdict, string> = {
  keep: 'worth the trip',
  check: 'look before you vendor',
  unpriceable: 'not in the price index — not worthless',
  trash: 'vendor',
  // Deliberately not an instruction. A quest item cannot be traded and cannot be
  // vendored, so any verb here would be one the game refuses to honour.
  not_loot: 'not loot — nothing to sell, nothing to vendor',
}

export const PROVENANCE_LABEL: Record<string, string> = {
  note: 'your ~price note',
  bulk: 'poe.ninja',
  exchange: 'bulk exchange',
  trade: 'trade search',
  unpriceable: 'no price',
}

/** Short form, for a row where the long form does not fit. */
export const PROVENANCE_SHORT: Record<string, string> = {
  note: 'note',
  bulk: 'ninja',
  exchange: 'exch',
  trade: 'trade',
  unpriceable: '—',
}
