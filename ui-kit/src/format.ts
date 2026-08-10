/**
 * Number formatting, in the kit because both profiles must agree.
 *
 * Every one of these is `tabular-nums` at the call site. The reason is a real
 * finding rather than typographic taste: a live bag holds
 * `Dead Man's Sulphur ×40296`, so a quantity column is five digits wide and a
 * proportional font makes each row a different width. Right-aligned tabular
 * figures are the only way a column of prices can be compared by eye.
 */

/** Chaos, at the precision a player can act on. Never scientific notation. */
export function formatChaos(chaos: number): string {
  const value = Math.abs(chaos)
  if (value === 0) return '0'
  if (value < 0.1) return chaos.toFixed(3).replace(/0+$/, '').replace(/\.$/, '')
  if (value < 10) return trimZeros(chaos.toFixed(2))
  if (value < 1000) return trimZeros(chaos.toFixed(1))
  return Math.round(chaos).toLocaleString('en-US')
}

export function formatDivine(divine: number): string {
  return divine < 10 ? trimZeros(divine.toFixed(2)) : trimZeros(divine.toFixed(1))
}

/**
 * What the value column says when it is not saying a number.
 *
 * Three different silences, and the first live appraisal proved they cannot share a
 * glyph: two rares whose trade searches had already returned zero results were drawn
 * with `⋯`, which promises a number that is never going to arrive. `∅` is the
 * terminal answer — *we asked, nothing matched* — and `—` stays what it always was:
 * no price, and none was ever sought.
 */
export const PRICE_PENDING = '⋯'
export const PRICE_NO_LISTINGS = '∅'
export const PRICE_NONE = '—'

interface PriceCell {
  chaos?: number | null
  pricing?: boolean
  noListings?: boolean
}

/**
 * One price cell, for both profiles and every field list.
 *
 * A shared function rather than four copies of the same ternary, because four copies
 * is how `∅` reaches three renderers and quietly misses the fourth.
 */
export function formatPriceCell(price: PriceCell | null | undefined, digits = 1): string {
  if (price?.pricing) return PRICE_PENDING
  if (price?.noListings) return PRICE_NO_LISTINGS
  if (price && price.chaos !== null && price.chaos !== undefined) {
    return `${price.chaos.toLocaleString('en-US', { maximumFractionDigits: digits })}c`
  }
  return PRICE_NONE
}

/**
 * Is there a number for a provenance label to be about?
 *
 * A type predicate, so the caller that passes the check may also read the model —
 * a provenance span guarded by a boolean and then reaching for `price!.provenance`
 * is the same bug written twice.
 */
export function hasPrice<T extends PriceCell>(price: T | null | undefined): price is T {
  return Boolean(
    price &&
      !price.pricing &&
      !price.noListings &&
      price.chaos !== null &&
      price.chaos !== undefined,
  )
}

/** `×40296`, grouped. The multiplication sign, not the letter x. */
export function formatQuantity(quantity: number): string {
  return `×${quantity.toLocaleString('en-US')}`
}

/** `14:32`. Local time, because "no change since 14:32" is only useful locally. */
export function formatClock(iso: string | null | undefined, now?: Date): string {
  if (!iso) return '—'
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return '—'
  void now
  return `${pad(when.getHours())}:${pad(when.getMinutes())}`
}

/** `4s`, `3m`, `2h`, `5d` — one unit, no decimals, never "less than a minute ago". */
export function formatAge(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return 'never'
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return 'never'
  const seconds = Math.max(0, Math.round((now.getTime() - when.getTime()) / 1000))
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

/** `0:47`. A countdown the player watches, so seconds stay visible throughout. */
export function formatCountdown(seconds: number): string {
  const total = Math.max(0, Math.ceil(seconds))
  if (total < 60) return `${total}s`
  return `${Math.floor(total / 60)}:${pad(total % 60)}`
}

function trimZeros(text: string): string {
  return text.includes('.') ? text.replace(/0+$/, '').replace(/\.$/, '') : text
}

function pad(value: number): string {
  return value < 10 ? `0${value}` : `${value}`
}
