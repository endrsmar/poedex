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

export const VERDICTS: readonly Verdict[] = ['keep', 'check', 'trash', 'unpriceable'] as const

/** The order the player acts in: keep, check, unpriceable, trash. Unpriceable
 * sits above trash because an unknown is a thing to look at and a trash verdict is
 * a thing to stop looking at. */
export const VERDICT_ORDER: readonly Verdict[] = ['keep', 'check', 'unpriceable', 'trash'] as const

export const VERDICT_GLYPH: Record<Verdict, string> = {
  keep: '●',
  check: '◐',
  unpriceable: '?',
  trash: '·',
}

export const VERDICT_LABEL: Record<Verdict, string> = {
  keep: 'keep',
  check: 'check',
  unpriceable: 'unpriceable',
  trash: 'trash',
}

/** The one-line claim each verdict is making. Straight from `cli/appraise.py`. */
export const VERDICT_HEADLINE: Record<Verdict, string> = {
  keep: 'worth the trip',
  check: 'look before you vendor',
  unpriceable: 'not in the price index — not worthless',
  trash: 'vendor',
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
