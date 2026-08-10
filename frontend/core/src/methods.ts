/**
 * Typed wrappers over the method registry.
 *
 * One function per registered backend method, with the argument names the Python
 * signature actually uses. The value is not the typing so much as the *list*: this
 * file is the frontend's complete view of what the backend can be asked to do, and
 * anything not here is not reachable from a screen.
 *
 * **`credentials.session_id` is absent, and that is not an oversight.** The
 * `credentials` module never registers it — `methods()` returns `status`, `set`,
 * `clear`, `mark_ok`, `mark_rejected` and nothing else — so there is no name a
 * transport could call. A wrapper here would fail at runtime, which is the right
 * shape of failure, but the honest statement is that the getter does not exist.
 */

import type { Transport } from './transport'
import type { BagAppraisalPayload, ItemSet, ServerMeta } from './types/generated'

export interface AppraiseBagArgs {
  character?: string | null
  strictness?: 'generous' | 'strict' | null
  threshold_chaos?: number | null
  escalate?: boolean | null
}

export function createClient(transport: Transport) {
  return {
    transport,

    meta(): Promise<ServerMeta> {
      return transport.call<ServerMeta>('_server.meta')
    },

    appraisal: {
      /** One account request, plus a few trade requests for the bag's gated rares. */
      bag(args: AppraiseBagArgs = {}): Promise<BagAppraisalPayload> {
        return transport.call<BagAppraisalPayload>('appraisal.appraise_bag', { ...args })
      },
      /** Tier 2 for one item of the current bag, by uid. No pricing, no requests. */
      gate(uid: string, character?: string | null): Promise<Record<string, unknown>> {
        return transport.call('appraisal.gate', { uid, character: character ?? null })
      },
      settings(): Promise<Record<string, unknown>> {
        return transport.call('appraisal.settings')
      },
    },

    poeapi: {
      items(character?: string | null, refresh = false): Promise<ItemSet> {
        return transport.call<ItemSet>('poeapi.get_items', {
          character: character ?? null,
          refresh,
        })
      },
      characters(): Promise<Record<string, unknown>> {
        return transport.call('poeapi.get_characters')
      },
      limits(): Promise<Record<string, unknown>> {
        return transport.call('poeapi.limits')
      },
    },

    prices: {
      status(league?: string | null): Promise<Record<string, unknown>> {
        return transport.call('prices.status', { league: league ?? null })
      },
      refresh(force = false, league?: string | null): Promise<Record<string, unknown>> {
        return transport.call('prices.refresh', { force, league: league ?? null })
      },
    },

    credentials: {
      /** State and metadata. Never the value — see this file's docstring. */
      status(): Promise<Record<string, unknown>> {
        return transport.call('credentials.status')
      },
    },
  }
}

export type PoedexClient = ReturnType<typeof createClient>
