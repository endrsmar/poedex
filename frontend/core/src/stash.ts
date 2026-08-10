/**
 * The stash store: a digest of every tab, and at most one tab open at a time.
 *
 * It is a store rather than `useState` in a screen for the reason the bag store is
 * (`bag.ts`), plus one this screen has to itself:
 *
 * > **Opening a tab costs a request, and nothing else here may.** The digest is
 * > free — it reads the tab list and prices what is already cached — but
 * > `openTab` spends. So the two are separate calls with separate state, and there
 * > is no code path where rendering, re-rendering or selecting a row fetches
 * > anything. A screen that fetched on hover would be a 117-request auto-crawl
 * > with good intentions, which SPEC §6.6 forbids in exactly those words.
 *
 * The store also keeps the *previous* tab on screen while the next one loads, for
 * the same reason the bag grid dims rather than blanks: a panel that empties itself
 * on every press teaches the player that it is unreliable.
 */

import type { StashDigestPayload, TabAppraisalPayload } from './types/generated'
import { createStore, type ReadableStore } from './store'
import { TransportError, type Unsubscribe } from './transport'
import type { PoedexClient } from './methods'
import type { BagSync } from './bag'

export interface StashState {
  digest: StashDigestPayload | null
  /** The tab currently open, or `null` when the list is all there is. */
  tab: TabAppraisalPayload | null
  /** Which index the player asked for — set before `tab` arrives, so the row can
   * show as selected while the request is in flight. */
  openIndex: number | null
  sync: BagSync
  /** True while the *tab* is loading. The digest has its own `sync.state`. */
  loadingTab: boolean
}

export interface StashStoreOptions {
  client: PoedexClient
  now?: () => Date
  league?: string | null
}

const INITIAL_SYNC: BagSync = {
  state: 'stale',
  at: null,
  checkedAt: null,
  detail: 'not fetched yet',
  retryAfter: null,
}

export interface StashStore extends ReadableStore<StashState> {
  /** The tab list. Costs at most one request — the tab list itself. */
  refresh(force?: boolean): Promise<void>
  /** Read one tab. **This is the call that spends**, and only a press reaches it. */
  openTab(index: number, options?: { refresh?: boolean }): Promise<void>
  closeTab(): void
  listen(): Unsubscribe
}

export function createStashStore(options: StashStoreOptions): StashStore {
  const { client, now = () => new Date() } = options
  const store = createStore<StashState>({
    digest: null,
    tab: null,
    openIndex: null,
    sync: INITIAL_SYNC,
    loadingTab: false,
  })
  let inFlight: Promise<void> | null = null

  function fail(error: unknown, stamp: string): BagSync {
    const current = store.get().sync
    if (error instanceof TransportError && error.restricted) {
      return {
        ...current,
        state: 'restricted',
        checkedAt: stamp,
        detail: error.message,
        retryAfter: error.retryAfter,
      }
    }
    return {
      ...current,
      state: 'error',
      checkedAt: stamp,
      detail: error instanceof Error ? error.message : String(error),
      retryAfter: null,
    }
  }

  async function refresh(force = false): Promise<void> {
    if (inFlight) return inFlight
    const previous = store.get()
    store.set({ ...previous, sync: { ...previous.sync, state: 'syncing', detail: null } })
    inFlight = (async () => {
      try {
        const digest = await client.appraisal.stash(options.league ?? null, force)
        const stamp = now().toISOString()
        store.set({
          ...store.get(),
          digest,
          sync: {
            state: 'fresh',
            at: stamp,
            checkedAt: stamp,
            detail: null,
            retryAfter: null,
          },
        })
      } catch (error) {
        store.set({ ...store.get(), sync: fail(error, now().toISOString()) })
      } finally {
        inFlight = null
      }
    })()
    return inFlight
  }

  async function openTab(index: number, opts: { refresh?: boolean } = {}): Promise<void> {
    // The previous tab stays on screen until this one lands.
    store.set({ ...store.get(), openIndex: index, loadingTab: true })
    try {
      const tab = await client.appraisal.tab(index, {
        league: options.league ?? null,
        refresh: opts.refresh ?? false,
      })
      store.set({ ...store.get(), tab, loadingTab: false })
      // A tab that was just read changes its own digest row — its age, its value,
      // whether it is still `known: false`. Re-reading the digest costs nothing.
      void refresh()
    } catch (error) {
      store.set({
        ...store.get(),
        loadingTab: false,
        sync: fail(error, now().toISOString()),
      })
    }
  }

  function closeTab(): void {
    store.set({ ...store.get(), tab: null, openIndex: null })
  }

  function listen(): Unsubscribe {
    // Deliberately **not** subscribed to `sync_complete`. That fires on every bag
    // sync, and re-reading the digest on a zone change would be free today and an
    // invitation to make it not-free tomorrow. The stash is near-live and the player
    // is standing at it; a refresh here is a press.
    client.transport.connect()
    return () => {}
  }

  return { get: store.get, subscribe: store.subscribe, refresh, openTab, closeTab, listen }
}
