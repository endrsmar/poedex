/**
 * `@poedex/ui` — the primitives, and exactly one profile's implementation of them.
 *
 * `#profile` is a package `imports` subpath (see `ui-kit/package.json`) resolved at
 * **build time** by an export condition:
 *
 * ```
 * default          -> src/profiles/full     web: Vite, real CSS
 * poedex-compact   -> src/profiles/compact  Decky: @decky/ui, inline styles
 * ```
 *
 * A build selects with `resolve.conditions: ['poedex-compact', ...]` (Rollup and
 * Vite both honour it) and neither bundle carries the other implementation — which
 * is IMPLEMENTATION-PLAN §2.3's requirement, and the reason the compact bundle does
 * not ship a CSS file it cannot use.
 */

export * from './contracts'
export * from './profile'
export * from './registry'
export * from './verdict'
export * from './format'
export * from './sync'

export {
  Screen,
  Section,
  Stack,
  Row,
  Stat,
  Tally,
  ItemGrid,
  ItemRow,
  ValueBar,
  VerdictPill,
  Action,
  Focus,
  Detail,
  Pending,
  Empty,
  ErrorState,
  StaleBanner,
  PROFILE,
} from '#profile'
