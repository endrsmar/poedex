/**
 * `DeckyTransport` — Phase 7. A stub, on purpose.
 *
 * The shape is already known and is worth writing down now, because it is what
 * proves the `Transport` interface is not quietly an HTTP interface:
 *
 * * **Methods** go through `callable<[Args], Result>('module.method')` from
 *   `@decky/api`, which resolves against the same `MethodRegistry` the HTTP
 *   transport posts to. Same names, same arguments, same JSON.
 * * **Events** arrive through `addEventListener(topic, handler)` — Decky's
 *   `decky.emit()` push — instead of SSE. There is no connection to open, which is
 *   why `connect()` is allowed to be a no-op in the interface rather than returning
 *   a promise.
 *
 * It is a stub rather than an implementation because `@decky/api` only exists
 * inside the Decky plugin host: importing it here would break every web build and
 * every test, and vendoring a fake would be a fake this phase cannot check against
 * hardware. Constructing it throws, with the reason.
 */

import {
  ListenerSet,
  type EventListener,
  type Transport,
  type TransportState,
  type Unsubscribe,
} from './transport'

export interface DeckyBridge {
  call(method: string, params: Record<string, unknown>): Promise<unknown>
  addEventListener(topic: string, handler: (payload: Record<string, unknown>) => void): Unsubscribe
}

export class DeckyTransport implements Transport {
  readonly id = 'decky' as const
  readonly state: TransportState = 'open'

  private readonly listeners = new ListenerSet()

  constructor(private readonly bridge?: DeckyBridge) {
    if (!bridge) {
      throw new Error(
        'DeckyTransport is a Phase 7 stub: construct it with a bridge built from ' +
          '@decky/api inside the plugin host. There is no web fallback, deliberately.',
      )
    }
  }

  call<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    return this.bridge!.call(method, params) as Promise<T>
  }

  on(pattern: string, listener: EventListener): Unsubscribe {
    return this.listeners.add(pattern, listener)
  }

  connect(): void {
    // Decky pushes; there is nothing to open.
  }

  close(): void {
    // ...and nothing to close.
  }

  onStateChange(): Unsubscribe {
    return () => {}
  }
}
