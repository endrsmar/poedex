/**
 * The bag store: one appraisal, and an honest account of how old it is.
 *
 * The six sync states are the whole reason this is a store rather than a `useState`
 * in a screen. Five of them are trivial; `unchanged` is not, and it is the one that
 * matters:
 *
 * > A refresh that fetched successfully and got back exactly what it had before is
 * > **not** a refresh. Saying "refreshed 2s ago" there is technically true and
 * > practically a lie — the player asked *"did anything change"* and the answer was
 * > no. So the store keeps `changedAt` separately from `checkedAt` and the surface
 * > says **"no change since 14:32"**.
 *
 * That distinction cannot be made by the backend alone (it does not hold the
 * previous answer the *screen* is showing) and must not be made by a component
 * (there are two surfaces). It belongs here.
 */

import type { BagAppraisalPayload, SyncMeta } from './types/generated'
import { createStore, type ReadableStore } from './store'
import { TransportError, type Unsubscribe } from './transport'
import type { PoedexClient } from './methods'

/** Derived from the generated wire model, so the two lists cannot drift. */
export type SyncState = SyncMeta['state']

export interface BagSync {
  state: SyncState
  /** ISO time of the data on screen — the last answer that *differed*. */
  at: string | null
  /** ISO time of the last completed attempt, changed or not. */
  checkedAt: string | null
  detail: string | null
  retryAfter: number | null
}

export interface BagState {
  bag: BagAppraisalPayload | null
  sync: BagSync
  /** True from the moment a refresh starts until it settles. */
  busy: boolean
}

export interface BagStoreOptions {
  client: PoedexClient
  /** Injected so tests are not at the mercy of the clock. */
  now?: () => Date
  /**
   * Topics that mean "the bag may have changed". Phase 6's `gamelog` emits a zone
   * change, `poeapi` emits `sync_complete`; the store re-appraises on either
   * without a screen having to know they exist.
   */
  refreshOn?: string[]
  character?: string | null
}

const INITIAL_SYNC: BagSync = {
  state: 'stale',
  at: null,
  checkedAt: null,
  detail: 'not fetched yet',
  retryAfter: null,
}

export interface BagStore extends ReadableStore<BagState> {
  refresh(): Promise<void>
  /** Start listening for backend events. Returns the teardown. */
  listen(): Unsubscribe
}

export function createBagStore(options: BagStoreOptions): BagStore {
  const { client, now = () => new Date() } = options
  const refreshOn = options.refreshOn ?? ['sync_complete', 'appraisal.complete']
  const store = createStore<BagState>({ bag: null, sync: INITIAL_SYNC, busy: false })
  let signature: string | null = null
  let inFlight: Promise<void> | null = null

  async function refresh(): Promise<void> {
    if (inFlight) return inFlight
    const previous = store.get()
    store.set({
      ...previous,
      busy: true,
      // `syncing` keeps the bag on screen. Blanking the grid during a refresh is
      // how a surface teaches a player that it is unreliable.
      sync: { ...previous.sync, state: 'syncing', detail: null },
    })

    inFlight = (async () => {
      try {
        // No `escalate`: a refresh costs one account request and no trade
        // requests. Rares come back highlighted, and the player asks.
        const bag = await client.appraisal.bag({ character: options.character ?? null })
        const stamp = now().toISOString()
        const next = signatureOf(bag)
        const changed = next !== signature
        const state: SyncState = bag.stale ? 'stale' : changed ? 'fresh' : 'unchanged'
        const at = changed || !store.get().sync.at ? stamp : store.get().sync.at
        signature = next
        store.set({
          bag,
          busy: false,
          sync: {
            state,
            at,
            checkedAt: stamp,
            detail: bag.stale ? 'served from cache; nothing was fetched' : null,
            retryAfter: null,
          },
        })
      } catch (error) {
        const stamp = now().toISOString()
        const current = store.get()
        if (error instanceof TransportError && error.restricted) {
          store.set({
            ...current,
            busy: false,
            sync: {
              ...current.sync,
              state: 'restricted',
              checkedAt: stamp,
              detail: error.message,
              retryAfter: error.retryAfter,
            },
          })
          return
        }
        store.set({
          ...current,
          busy: false,
          sync: {
            ...current.sync,
            state: 'error',
            checkedAt: stamp,
            detail: error instanceof Error ? error.message : String(error),
            retryAfter: null,
          },
        })
      } finally {
        inFlight = null
      }
    })()

    return inFlight
  }

  function listen(): Unsubscribe {
    const offs = refreshOn.map((topic) =>
      client.transport.on(topic, () => {
        void refresh()
      }),
    )
    client.transport.connect()
    return () => {
      for (const off of offs) off()
    }
  }

  return { get: store.get, subscribe: store.subscribe, refresh, listen }
}

/**
 * What "the same bag" means.
 *
 * Not deep equality of the payload: `lookups`, `trade_requests` and the table
 * timestamps change on every pass and none of them is something the player can see
 * changing. This is the visible content — which rows exist, what they are worth,
 * and what the tool decided about them. Two rows of the same item with different
 * stack sizes stay two entries here, because they are two rows on screen.
 */
function signatureOf(bag: BagAppraisalPayload): string {
  const rows = (bag.items ?? [])
    .map(
      (item) =>
        `${item.uid}:${item.verdict}:${item.stack_size}:${round(item.total_chaos)}:${
          item.pricing ? 'p' : '-'
        }`,
    )
    .join('|')
  return `${bag.league}#${round(bag.total_chaos)}#${bag.counts.keep}/${bag.counts.check}/${
    bag.counts.trash
  }/${bag.counts.unpriceable}#${rows}`
}

function round(value: number): string {
  return value.toFixed(2)
}
