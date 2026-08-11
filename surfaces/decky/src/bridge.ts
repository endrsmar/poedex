/**
 * The only file in the frontend that imports `@decky/api`.
 *
 * `@poedex/core` is framework- *and* host-agnostic on purpose — it is the one thing
 * both surfaces share — so `DeckyTransport` takes a {@link DeckyBridge} rather than
 * importing Decky's RPC itself. This is the fifteen lines that build one.
 *
 * `callable(route)` resolves against the plugin's `Plugin` class by method name and
 * passes **positional** arguments, so the backend exposes one door (`call(method,
 * params)`) instead of twenty. `addEventListener(name, handler)` receives
 * `decky.emit()` pushes and returns the handler, not an unsubscribe, so the
 * unsubscribe is composed here.
 */

import { addEventListener, callable, removeEventListener } from '@decky/api'
import { DeckyTransport, createClient } from '@poedex/core'
import type { DeckyBridge, DeckyEnvelope, PoedexClient } from '@poedex/core'

/** The event name `decky.emit()` is called with in `transports/decky/backend.py`. */
export const CHANNEL = 'poedex'

const callBackend = callable<[string, Record<string, unknown>], DeckyEnvelope>('call')
const latestBackend = callable<[string | null], Record<string, unknown>>('latest')

export function createBridge(): DeckyBridge {
  return {
    call: (method, params) => callBackend(method, params),
    addEventListener: (handler) => {
      const listener = addEventListener<[unknown]>(CHANNEL, handler)
      return () => removeEventListener(CHANNEL, listener)
    },
    latest: (topic) => latestBackend(topic ?? null),
  }
}

/**
 * The client every module screen reaches through `getClient()`.
 *
 * Built once at plugin definition rather than per panel mount: SPEC §6.2 —
 * panel content is unmounted whenever the QAM closes, so anything that should
 * survive that has to live outside the React tree.
 */
export function createDeckyClient(): PoedexClient {
  const transport = new DeckyTransport(createBridge())
  transport.connect()
  return createClient(transport)
}
