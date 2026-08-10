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
import type {
  BagAppraisalPayload,
  ItemHighlightPayload,
  ItemSet,
  PriceCheckPayload,
  ServerMeta,
  StashDigestPayload,
  TabAppraisalPayload,
} from './types/generated'

export interface AppraiseBagArgs {
  character?: string | null
  strictness?: 'generous' | 'strict' | null
  threshold_chaos?: number | null
}

/**
 * What the player ticked. Indexes, never mod text.
 *
 * `mods: null` means "the pre-ticked set"; `mods: []` means "none of them", which is
 * how a pure open-affix question is asked. The two are different queries and the
 * backend treats them as such.
 */
export interface PriceCheckArgs {
  mods?: number[] | null
  open_prefixes?: number | null
  open_suffixes?: number | null
  character?: string | null
  league?: string | null
  /**
   * Which stash tab the item is in. Omitted, the uid is looked for in the bag.
   *
   * Required rather than optional-with-a-search on purpose: scanning every tab for a
   * uid would be up to 117 requests behind a call that looks free, and a surface that
   * showed the item knows which tab it came from.
   */
  tab_index?: number | null
}

export function createClient(transport: Transport) {
  return {
    transport,

    meta(): Promise<ServerMeta> {
      return transport.call<ServerMeta>('_server.meta')
    },

    appraisal: {
      /**
       * One account request and **zero** trade requests.
       *
       * Rares are highlighted, never auto-priced — IMPLEMENTATION-PLAN §5b. There is
       * no `escalate` argument any more because there is no path it could switch on.
       */
      bag(args: AppraiseBagArgs = {}): Promise<BagAppraisalPayload> {
        return transport.call<BagAppraisalPayload>('appraisal.appraise_bag', { ...args })
      },
      /** Tier 2 for one item of the current bag, by uid. No pricing, no requests. */
      gate(uid: string, character?: string | null): Promise<Record<string, unknown>> {
        return transport.call('appraisal.gate', { uid, character: character ?? null })
      },
      /** The checkbox list for one item: its mods, their tiers, its open affixes.
       * Local and free — `moddb` is a file on disk.
       *
       * The **same** call for a bag item and a stash item; `tabIndex` only says where
       * to find the uid. Phase 10 extends the check to the stash by not forking it. */
      highlight(
        uid: string,
        character?: string | null,
        tabIndex?: number | null,
      ): Promise<ItemHighlightPayload> {
        return transport.call<ItemHighlightPayload>('appraisal.highlight', {
          uid,
          character: character ?? null,
          tab_index: tabIndex ?? null,
        })
      },
      /** Ask the market the player's question. **The only call here that spends.** */
      priceCheck(uid: string, args: PriceCheckArgs = {}): Promise<PriceCheckPayload> {
        return transport.call<PriceCheckPayload>('appraisal.price_check', {
          uid,
          mods: args.mods ?? null,
          open_prefixes: args.open_prefixes ?? null,
          open_suffixes: args.open_suffixes ?? null,
          character: args.character ?? null,
          league: args.league ?? null,
          tab_index: args.tab_index ?? null,
        })
      },
      /**
       * Every stash tab, with what is already known about each. **No item requests.**
       *
       * Opening the stash screen must never become a crawl: this reads the tab list
       * (cached 15 minutes) and prices only the tabs already on disk. Tabs nobody has
       * read come back `known: false`, which is *unknown* and not zero.
       */
      stash(league?: string | null, refresh = false): Promise<StashDigestPayload> {
        return transport.call<StashDigestPayload>('appraisal.stash_digest', {
          league: league ?? null,
          refresh,
        })
      },
      /**
       * One tab, judged at stash strictness. **One request, about a second** — and
       * none at all when the tab is remove-only and already cached.
       */
      tab(
        tabIndex: number,
        args: { league?: string | null; strictness?: 'generous' | 'strict' | null; refresh?: boolean } = {},
      ): Promise<TabAppraisalPayload> {
        return transport.call<TabAppraisalPayload>('appraisal.appraise_tab', {
          tab_index: tabIndex,
          league: args.league ?? null,
          strictness: args.strictness ?? null,
          refresh: args.refresh ?? false,
        })
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
