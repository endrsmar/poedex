/**
 * Surface profiles (IMPLEMENTATION-PLAN §2.2) and the per-profile hint.
 *
 * A hint is how a module declares *how much* to show at each density without
 * knowing which density it is running at:
 *
 * ```tsx
 * <Section limit={{ compact: 5, full: null }}>
 * ```
 *
 * The alternative — `useProfile()` and an `if` in the module — was rejected on
 * purpose. It puts a surface decision inside the feature, it is invisible to
 * anything reading the JSX, and it makes every screen a place where `compact` can
 * be forgotten. A hint is a value: the kit can read it, a test can assert on it,
 * and the two densities sit next to each other where they can be compared.
 */

export type ProfileId = 'compact' | 'full'

export interface Profile {
  id: ProfileId
  /** 300 for the Decky QAM; `'fluid'` for a browser window. */
  width: number | 'fluid'
  input: 'gamepad' | 'pointer'
  density: 'compact' | 'comfortable'
}

export const COMPACT_PROFILE: Profile = {
  id: 'compact',
  width: 300,
  input: 'gamepad',
  density: 'compact',
}

export const FULL_PROFILE: Profile = {
  id: 'full',
  width: 'fluid',
  input: 'pointer',
  density: 'comfortable',
}

/**
 * A value, or one value per profile.
 *
 * The object form is deliberately total — both keys required — so adding a
 * profile is a type error at every site that declares a density, rather than a
 * silent fallback to whatever `full` wanted.
 */
export type Hint<T> = T | { compact: T; full: T }

function isHintMap<T>(hint: Hint<T>): hint is { compact: T; full: T } {
  return (
    typeof hint === 'object' &&
    hint !== null &&
    'compact' in (hint as object) &&
    'full' in (hint as object)
  )
}

export function resolveHint<T>(hint: Hint<T> | undefined, profile: ProfileId, fallback: T): T {
  if (hint === undefined) return fallback
  if (isHintMap(hint)) return hint[profile]
  return hint
}
