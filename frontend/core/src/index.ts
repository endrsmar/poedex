/**
 * `@poedex/core` — the framework-agnostic frontend runtime.
 *
 * Transports, typed method wrappers, observable stores. **No React**, so both
 * surfaces can use it: Steam's gaming-mode UI runs its own React and a runtime that
 * imported one would have to pick.
 */

export * from './transport'
export * from './http'
export * from './decky'
export * from './store'
export * from './methods'
export * from './runtime'
export * from './bag'
export * from './stash'
export type * from './types/generated'
