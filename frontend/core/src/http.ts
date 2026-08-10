/**
 * `HttpTransport` — fetch for methods, SSE for events.
 *
 * Talks to `transports/http`, which binds to 127.0.0.1 only. Two things it
 * deliberately does not do:
 *
 * * **No credential.** There is no method that returns a `POESESSID` — the
 *   `credentials` module does not register its `session_id` accessor, and this
 *   transport has no special path that could reach one. That is not a policy this
 *   file enforces; it is the absence of a getter, which is stronger.
 * * **No retry loop on a refusal.** `net` refuses rather than queueing, and a
 *   frontend that retries on its own behalf turns one refusal into a pattern that
 *   gets an account restricted. A `429` surfaces as `TransportError.restricted`
 *   with the limiter's `Retry-After`, and the UI waits it out visibly.
 */

import {
  ListenerSet,
  TransportError,
  type EventListener,
  type Transport,
  type TransportEvent,
  type TransportState,
  type Unsubscribe,
} from './transport'

export interface EventSourceLike {
  addEventListener(type: string, listener: (event: MessageEvent) => void): void
  close(): void
  onerror: ((event: Event) => void) | null
  onopen: ((event: Event) => void) | null
}

export interface HttpTransportOptions {
  /** Defaults to the page's own origin, which is the only thing it should ever be. */
  baseUrl?: string
  fetch?: typeof globalThis.fetch
  /** Injected so a test can drive the SSE stream without a server or a network. */
  eventSource?: (url: string) => EventSourceLike
  /** Milliseconds between reconnect attempts. Backs off to 30 s. */
  reconnectDelay?: number
  setTimeout?: typeof globalThis.setTimeout
}

export class HttpTransport implements Transport {
  readonly id = 'http' as const

  private readonly baseUrl: string
  private readonly doFetch: typeof globalThis.fetch
  private readonly makeEventSource: (url: string) => EventSourceLike
  private readonly listeners = new ListenerSet()
  private readonly stateListeners = new Set<(state: TransportState) => void>()
  private readonly reconnectDelay: number
  private readonly schedule: typeof globalThis.setTimeout

  private source: EventSourceLike | null = null
  private _state: TransportState = 'idle'
  private attempts = 0
  private closedByCaller = false

  constructor(options: HttpTransportOptions = {}) {
    this.baseUrl = (options.baseUrl ?? '').replace(/\/$/, '')
    this.doFetch = options.fetch ?? globalThis.fetch.bind(globalThis)
    this.reconnectDelay = options.reconnectDelay ?? 1000
    this.schedule = options.setTimeout ?? globalThis.setTimeout.bind(globalThis)
    this.makeEventSource =
      options.eventSource ??
      ((url: string) => new globalThis.EventSource(url) as unknown as EventSourceLike)
  }

  get state(): TransportState {
    return this._state
  }

  async call<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    let response: Response
    try {
      response = await this.doFetch(`${this.baseUrl}/api/call/${encodeURIComponent(method)}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(params),
      })
    } catch (error) {
      throw new TransportError(
        `cannot reach the PoEDex backend — is it running? (${String(error)})`,
        { kind: 'Unreachable' },
      )
    }

    let body: unknown = null
    try {
      body = await response.json()
    } catch {
      throw new TransportError(`${method}: the backend returned a non-JSON response`, {
        kind: 'MalformedResponse',
        status: response.status,
      })
    }

    const envelope = body as { ok?: boolean; result?: T; error?: WireError }
    if (!envelope || typeof envelope !== 'object' || envelope.ok === undefined) {
      throw new TransportError(`${method}: unrecognised response envelope`, {
        kind: 'MalformedResponse',
        status: response.status,
      })
    }
    if (envelope.ok) return envelope.result as T

    const error = envelope.error ?? { kind: 'Error', message: 'unspecified failure' }
    throw new TransportError(error.message, {
      kind: error.kind,
      retryAfter: error.retry_after ?? readRetryAfter(response),
      status: response.status,
    })
  }

  on(pattern: string, listener: EventListener): Unsubscribe {
    return this.listeners.add(pattern, listener)
  }

  connect(): void {
    if (this.source || this._state === 'connecting') return
    this.closedByCaller = false
    this.setState('connecting')
    const source = this.makeEventSource(`${this.baseUrl}/api/events`)
    this.source = source

    source.onopen = () => {
      this.attempts = 0
      this.setState('open')
    }
    source.addEventListener('message', (event: MessageEvent) => {
      const parsed = parseEvent(event.data)
      if (parsed) this.listeners.emit(parsed)
    })
    source.onerror = () => {
      // EventSource reconnects on its own for transport hiccups, but a backend that
      // went away needs a visible state rather than a silent gap.
      this.setState('error')
      this.source?.close()
      this.source = null
      if (this.closedByCaller) return
      this.attempts += 1
      const delay = Math.min(30_000, this.reconnectDelay * 2 ** Math.min(this.attempts, 5))
      this.schedule(() => {
        if (!this.closedByCaller) this.connect()
      }, delay)
    }
  }

  close(): void {
    this.closedByCaller = true
    this.source?.close()
    this.source = null
    this.setState('closed')
  }

  onStateChange(listener: (state: TransportState) => void): Unsubscribe {
    this.stateListeners.add(listener)
    return () => this.stateListeners.delete(listener)
  }

  private setState(state: TransportState): void {
    if (this._state === state) return
    this._state = state
    for (const listener of this.stateListeners) listener(state)
  }
}

interface WireError {
  kind: string
  message: string
  retry_after?: number | null
}

function parseEvent(data: unknown): TransportEvent | null {
  if (typeof data !== 'string') return null
  try {
    const parsed = JSON.parse(data) as Partial<TransportEvent>
    if (!parsed || typeof parsed.topic !== 'string') return null
    return {
      topic: parsed.topic,
      payload: (parsed.payload as Record<string, unknown>) ?? {},
      source: parsed.source ?? null,
      at: parsed.at,
    }
  } catch {
    return null
  }
}

function readRetryAfter(response: Response): number | null {
  const header = response.headers?.get?.('retry-after')
  if (!header) return null
  const seconds = Number(header)
  return Number.isFinite(seconds) ? seconds : null
}
