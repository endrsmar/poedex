/**
 * The one thing a module UI is allowed to reach for.
 *
 * A screen needs a client, and the boundary rule (IMPLEMENTATION-PLAN §2.6) says a
 * module's UI may import `@poedex/ui`, its own types, and the frontend runtime —
 * and nothing else. In particular it may not import the shell, so a React context
 * owned by `surfaces/web` is not available to it, and a context owned by the kit
 * would put a transport inside the component library.
 *
 * So the runtime holds it: the shell installs a client at startup, and a screen
 * asks for it. A module-level singleton is the right shape here for the same reason
 * SPEC §6.2 gives for the Decky panel — panel content is unmounted whenever the QAM
 * closes, so state that survives has to live outside the tree.
 */

import type { PoedexClient } from './methods'

let installed: PoedexClient | null = null

export function installClient(client: PoedexClient): void {
  installed = client
}

/** For tests and for a shell tearing down. */
export function clearClient(): void {
  installed = null
}

export function getClient(): PoedexClient {
  if (!installed) {
    throw new Error(
      'no PoEDex client installed — a surface shell must call installClient() ' +
        'before it mounts a module screen',
    )
  }
  return installed
}

export function hasClient(): boolean {
  return installed !== null
}
