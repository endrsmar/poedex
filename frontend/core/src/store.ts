/**
 * A minimal observable store.
 *
 * Deliberately not a state library. React is not a dependency of this package —
 * the shells bind to it with `useSyncExternalStore`, which needs exactly
 * `subscribe` and `getSnapshot` and nothing else. Anything more here would be a
 * choice made on behalf of both surfaces by the layer least able to judge it.
 */

export type Listener = () => void

/** Re-uses the transport's alias so the package exports one `Unsubscribe`. */
import type { Unsubscribe } from './transport'
export type { Unsubscribe }

export interface ReadableStore<T> {
  get(): T
  subscribe(listener: Listener): Unsubscribe
}

export interface Store<T> extends ReadableStore<T> {
  set(next: T): void
  update(fn: (current: T) => T): void
}

export function createStore<T>(initial: T): Store<T> {
  let value = initial
  const listeners = new Set<Listener>()

  return {
    get: () => value,
    set(next: T) {
      if (Object.is(next, value)) return
      value = next
      for (const listener of [...listeners]) listener()
    },
    update(fn) {
      this.set(fn(value))
    },
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}
