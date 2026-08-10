/**
 * The transport contract.
 *
 * A transport is a thin adapter over the Python method registry and the runtime
 * event bus, and it is the *only* thing in the frontend that knows how bytes move.
 * There are two: `HttpTransport` (Phase 5, this file's sibling) and
 * `DeckyTransport` (Phase 7, `decky.ts`). Everything above this line — stores,
 * typed wrappers, every screen — is written against the interface and does not
 * change when the surface does.
 *
 * No React. This whole package is framework-agnostic on purpose: `@decky/ui` runs
 * on Steam's React 19 and the web shell on its own, and a runtime that imported
 * React would have to pick one.
 */

export interface TransportEvent {
  /** Dotted topic: `sync_complete`, `appraisal.complete`, `gamelog.zone_entered`. */
  topic: string
  payload: Record<string, unknown>
  source?: string | null
  /** Epoch seconds, as the backend saw it. */
  at?: number
}

export type EventListener = (event: TransportEvent) => void
export type Unsubscribe = () => void

/** Connection state of the event channel. The method channel is stateless. */
export type TransportState = 'idle' | 'connecting' | 'open' | 'closed' | 'error'

export interface Transport {
  readonly id: 'http' | 'decky'
  /**
   * Invoke `module.method` with keyword arguments.
   *
   * Rejects with a {@link TransportError}. There is no overload that returns a
   * result-or-error union: a caller that forgets to check one is a caller that
   * renders `undefined` as a price.
   */
  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>
  /** Subscribe to backend events. Returns the unsubscribe. */
  on(pattern: string, listener: EventListener): Unsubscribe
  /** Open the event channel. Idempotent. */
  connect(): void
  close(): void
  readonly state: TransportState
  onStateChange(listener: (state: TransportState) => void): Unsubscribe
}

/**
 * A failed call, with enough structure to act on.
 *
 * `kind` is the Python exception class name off the wire. `retryAfter` is the
 * limiter's own number and is what turns an error into the `restricted` sync state
 * with a live countdown, rather than a red box that says "try again" while every
 * attempt is refused.
 */
export class TransportError extends Error {
  readonly kind: string
  readonly retryAfter: number | null
  readonly status: number | null

  constructor(
    message: string,
    options: { kind?: string; retryAfter?: number | null; status?: number | null } = {},
  ) {
    super(message)
    this.name = 'TransportError'
    this.kind = options.kind ?? 'Error'
    this.retryAfter = options.retryAfter ?? null
    this.status = options.status ?? null
  }

  /** Did the rate limiter refuse this? `net` refuses rather than queueing. */
  get restricted(): boolean {
    return this.kind === 'RateLimited' || this.kind === 'RateLimitedError' || this.status === 429
  }
}

/** Matches the event bus's own rules: exact, `prefix.*`, or `*`. */
export function topicMatches(pattern: string, topic: string): boolean {
  if (pattern === '*' || pattern === topic) return true
  return pattern.endsWith('.*') && topic.startsWith(pattern.slice(0, -1))
}

/** Shared listener bookkeeping, so both transports behave identically. */
export class ListenerSet {
  private entries: { pattern: string; listener: EventListener }[] = []

  add(pattern: string, listener: EventListener): Unsubscribe {
    const entry = { pattern, listener }
    this.entries.push(entry)
    return () => {
      this.entries = this.entries.filter((candidate) => candidate !== entry)
    }
  }

  emit(event: TransportEvent): void {
    for (const entry of this.entries) {
      if (!topicMatches(entry.pattern, event.topic)) continue
      try {
        entry.listener(event)
      } catch (error) {
        // An event is a notification, not a call — the same rule the Python bus
        // follows. One broken listener does not stop the others.
        console.error('poedex: event listener failed', error)
      }
    }
  }

  get size(): number {
    return this.entries.length
  }
}
