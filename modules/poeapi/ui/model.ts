/**
 * The picker's opinions, kept out of the component so they can be read and tested
 * as sentences rather than as a rendered tree.
 *
 * There is one opinion here and everything else follows from it: **a derived answer
 * and a chosen one must not look the same.** The bug this screen closes was not that
 * the tool picked the wrong character — it was that "PlaceholderWarden" appeared in the
 * header with exactly the confidence it would have had if somebody had chosen it.
 */

import type { CheckOptionModel, Tone } from '@poedex/ui'
import type { Character, CharacterSelection, CharacterSource } from '@poedex/core'

/** How each rule reads to a player. Keyed by the enum, so a source added on the
 * Python side without a sentence here is a type error rather than a blank line. */
export const REASON: Record<CharacterSource, string> = {
  argument: 'named on the command line',
  environment: 'set by POEDEX_CHARACTER for this session',
  setting: 'you picked this one',
  current: 'the character you are playing',
  last_login: 'most recently played',
  fallback:
    'GUESSED — nothing on this account says which character to read, so this is ' +
    'only the first one the API listed. Pick one below.',
  none: 'this account has no characters',
}

/**
 * `warn` for a guess and for nothing else.
 *
 * Not for a pin that disagrees with the account, and not for `current` disagreeing
 * with `last_login`: reading a character other than the one you played last is an
 * ordinary thing to want, and colouring it as a problem teaches a player to ignore
 * the colour on the one screen where it means something.
 */
export function toneOf(selection: CharacterSelection): Tone {
  return selection.choice.source === 'fallback' || selection.choice.name === null
    ? 'warn'
    : 'neutral'
}

/** The sentence under the name. Never the name on its own. */
export function reasonOf(selection: CharacterSelection): string {
  const { choice } = selection
  const base = REASON[choice.source]
  if (choice.played_last && choice.played_last !== choice.name) {
    return `${base} · you last played ${choice.played_last}`
  }
  return base
}

/**
 * `2026-08-11 21:40` → `yesterday`.
 *
 * Relative, because the question a player is answering while looking at this list is
 * "which of these was I on last night", and nobody holds a timestamp in their head.
 * The absolute form stays in `meta` for the profile that has room for it.
 */
export function playedAgo(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return 'never played'
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return 'never played'
  // Calendar days in UTC, not elapsed hours. "Yesterday evening" is fourteen hours
  // ago at lunchtime and the player still calls it yesterday; a duration would call
  // it today, which is the one word that makes them stop trusting the column.
  const days = Math.round((utcDay(now) - utcDay(when)) / 86_400_000)
  if (days < 0) return 'in the future?'
  if (days === 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 60) return `${days} days ago`
  return `${Math.floor(days / 30)} months ago`
}

/**
 * One row per character. **The league is the badge**, and that is the whole design.
 *
 * `compact` keeps the badge and drops the meta, which is exactly the right trade
 * here: three characters differing only by league is the ordinary case, and the
 * league is the column whose absence made reading a parked Standard character look
 * reasonable for weeks. Class and level are context; the league is the answer.
 */
export function toOptions(
  selection: CharacterSelection,
  now: Date = new Date(),
): CheckOptionModel[] {
  return (selection.characters ?? []).map((character) => ({
    id: character.name,
    label: character.name,
    badge: character.league ?? 'league unknown',
    meta: metaOf(character, now),
    tone: character.name === selection.configured ? 'accent' : 'neutral',
  }))
}

function utcDay(when: Date): number {
  return Date.UTC(when.getUTCFullYear(), when.getUTCMonth(), when.getUTCDate())
}

function metaOf(character: Character, now: Date): string {
  const parts = [character.class_name ?? 'unknown class']
  if (character.level) parts.push(`level ${character.level}`)
  parts.push(playedAgo(character.last_login, now))
  return parts.join(' · ')
}

/**
 * What pressing a row means.
 *
 * Pressing the row you already pinned clears the pin — an override you cannot undo
 * from the surface that set it is a trap, and on a Deck this surface is the only one
 * there is. Pressing any other row pins it. `null` is "follow the account again".
 */
export function nextPin(selection: CharacterSelection, id: string): string | null {
  return selection.configured === id ? null : id
}
