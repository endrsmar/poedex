/**
 * `DeckyTransport` — the method registry and the event bus, over Decky's RPC.
 *
 * Phase 5 left this a stub with the shape written down. Phase 7 implements it, and
 * the shape turned out to be right: nothing above this line changed. Stores, typed
 * wrappers and every screen are written against `Transport` and do not know which
 * one they got.
 *
 * ## Two doors, and both of them are narrow
 *
 * * **Methods.** `@decky/api`'s `callable<[Args], Result>(route)` invokes a
 *   same-named coroutine on the plugin's `Plugin` class with **positional**
 *   arguments. A registry of twenty namespaced methods taking keyword arguments does
 *   not fit that, so the backend exposes exactly one — `call(method, params)` — and
 *   this is the other side of it. The envelope it returns (`ok`, `result`, `error`,
 *   `status`, `retry_after`) is turned back into a resolved value or a
 *   {@link TransportError}, so a caller cannot tell the two transports apart.
 *
 * * **Events.** `addEventListener('poedex', handler)` receives `decky.emit()`
 *   pushes. There is no connection to open, which is why `connect()` is allowed to
 *   be a no-op in the interface rather than returning a promise — and why `state` is
 *   `'open'` from construction. Calling this "connected" is not a lie by omission:
 *   the backend is a separate OS process that was already running before the panel
 *   mounted, so there is genuinely nothing to wait for.
 *
 * ## Why the bridge is injected
 *
 * `@decky/api` only exists inside the plugin host. Importing it here would break
 * every web build and every test, and this package is deliberately
 * framework-and-host agnostic — it is the one thing both surfaces share. So the
 * Decky shell (`surfaces/decky`) builds a {@link DeckyBridge} out of `callable` and
 * `addEventListener` and hands it over. Three functions; `createDeckyTransport` in
 * the shell is the only place `@decky/api` is named.
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

/** The envelope `Plugin.call` returns. Mirrors `DispatchResult` in Python. */
export interface DeckyEnvelope {
  ok: boolean
  result?: unknown
  error?: { kind?: string; message?: string; retry_after?: number | null } | null
  status?: number
  retry_after?: number | null
}

/**
 * What the Decky shell wires up out of `@decky/api`.
 *
 * Deliberately three functions rather than the `@decky/api` module itself: a fake is
 * then three lines in a test, and the shape of what this transport needs is legible
 * without reading Decky's types.
 */
export interface DeckyBridge {
  /** `callable<[string, Record<string, unknown>], DeckyEnvelope>('call')` */
  call(method: string, params: Record<string, unknown>): Promise<DeckyEnvelope | unknown>
  /** `addEventListener('poedex', handler)`; returns the unsubscribe. */
  addEventListener(handler: (message: unknown) => void): Unsubscribe
  /** `callable<[string | null], Record<string, TransportEvent>>('latest')`, optional.
   *
   * Panel content is unmounted whenever the QAM closes (SPEC §6.2), so a screen that
   * mounts after `sync_complete` fired would otherwise sit on a spinner until the
   * next zone change. This replays what the backend already knows. Optional because
   * a bridge without it is still a working transport, just a colder one. */
  latest?(topic?: string | null): Promise<Record<string, unknown>>
}

export class DeckyTransport implements Transport {
  readonly id = 'decky' as const

  private readonly listeners = new ListenerSet()
  private detach: Unsubscribe | null = null
  private connected = false

  constructor(private readonly bridge: DeckyBridge) {
    if (!bridge || typeof bridge.call !== 'function') {
      throw new Error(
        'DeckyTransport needs a bridge built from @decky/api inside the plugin host. ' +
          'There is no web fallback, deliberately: a fake would be a fake this project ' +
          'cannot check against hardware.',
      )
    }
  }

  /**
   * The backend is a separate process that outlives the panel, so there is no
   * connecting state to report and never a `closed` one worth acting on.
   */
  get state(): TransportState {
    return 'open'
  }

  async call<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    let envelope: DeckyEnvelope
    try {
      envelope = (await this.bridge.call(method, params)) as DeckyEnvelope
    } catch (error) {
      // The RPC itself failed — the backend process is gone, or Decky's websocket
      // dropped. That is a different failure from a method that raised, and it is
      // worth saying so: one is "try again", the other is "the plugin is not there".
      throw new TransportError(error instanceof Error ? error.message : String(error), {
        kind: 'TransportUnavailable',
      })
    }
    if (!envelope || typeof envelope !== 'object') {
      throw new TransportError(`${method} returned nothing`, { kind: 'ProtocolError' })
    }
    if (envelope.ok) return envelope.result as T
    const error = envelope.error ?? {}
    throw new TransportError(error.message ?? `${method} failed`, {
      kind: error.kind ?? 'Error',
      retryAfter: error.retry_after ?? envelope.retry_after ?? null,
      status: envelope.status ?? null,
    })
  }

  on(pattern: string, listener: EventListener): Unsubscribe {
    return this.listeners.add(pattern, listener)
  }

  /**
   * Attach to the push channel. Idempotent.
   *
   * Decky pushes whether or not anyone asked, so this only decides whether the
   * pushes reach the listener set. It is still called `connect` because the
   * `Transport` contract says so and because a surface should not have to know which
   * transport it installed.
   */
  connect(): void {
    if (this.connected) return
    this.connected = true
    this.detach = this.bridge.addEventListener((message) => this.receive(message))
    // Ask for what already happened. A panel opening for the second time in a
    // session mounts into a backend that has been syncing for an hour.
    if (this.bridge.latest) {
      void this.bridge
        .latest()
        .then((known) => {
          for (const message of Object.values(known ?? {})) this.receive(message)
        })
        .catch(() => {
          // A replay that fails costs freshness, never correctness — the next real
          // event arrives on the same channel.
        })
    }
  }

  close(): void {
    this.detach?.()
    this.detach = null
    this.connected = false
  }

  /**
   * The state never changes, so a listener would never fire.
   *
   * Returning a no-op unsubscribe rather than throwing: the web shell renders a
   * `live`/`connecting` chip off this, and a surface that shares that code should
   * get "always live" rather than a crash.
   */
  onStateChange(): Unsubscribe {
    return () => {}
  }

  private receive(message: unknown): void {
    const event = asTransportEvent(message)
    if (event) this.listeners.emit(event)
  }
}

/** One `decky.emit('poedex', …)` payload, if it is one. */
function asTransportEvent(message: unknown): TransportEvent | null {
  if (!message || typeof message !== 'object') return null
  const candidate = message as Record<string, unknown>
  if (typeof candidate.topic !== 'string') return null
  const payload = candidate.payload
  return {
    topic: candidate.topic,
    payload: payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {},
    source: typeof candidate.source === 'string' ? candidate.source : null,
    at: typeof candidate.at === 'number' ? candidate.at : undefined,
  }
}
