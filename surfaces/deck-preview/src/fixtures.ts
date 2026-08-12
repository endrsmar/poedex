/**
 * The preview's offline half: the committed fixtures, behind the `Transport` contract.
 *
 * The preview talks to a running `poedex serve` when there is one, because a panel
 * previewed against invented data is a panel previewed against the wrong data. But a
 * backend needs a POESESSID and spends rate-limit budget, and most of what this tool
 * is for — geometry, focus, what fits at 300 px — needs neither. So with no backend
 * up, this answers from `modules/<id>/ui/fixtures`: the *same* files the component
 * tests use, written by `scripts/make_ui_fixtures.py` out of the real payload classes,
 * with a Python test that fails when they drift.
 *
 * Two rules it follows, both of which the preview shell then states on screen:
 *
 * * **It never invents a payload the backend could not produce.** Where a screen needs
 *   something no fixture covers — a credential status, a pairing window — the value
 *   here is minimal, obviously fake (`Preview#0000`), and marked as such.
 * * **It is slow on purpose where the real thing is slow.** `price_check` is the one
 *   call that spends a trade request, and its `pricing…` state is a thing the panel
 *   draws. Answering it instantly would hide the state that took two bugs to get right.
 *
 * Nothing here touches the network, and nothing here is imported by the plugin bundle:
 * `scripts/build_plugin.py` copies four Python packages and one JS file, and this is
 * neither.
 */

import { ListenerSet, TransportError } from '@poedex/core'
import type {
  EventListener,
  Transport,
  TransportState,
  Unsubscribe,
} from '@poedex/core'

import bagFixture from '../../../modules/appraisal/ui/fixtures/bag-appraisal.json'
import highlightFixture from '../../../modules/appraisal/ui/fixtures/item-highlight.json'
import checkFixture from '../../../modules/appraisal/ui/fixtures/price-check.json'
import stashDigestFixture from '../../../modules/appraisal/ui/fixtures/stash-digest.json'
import stashTabFixture from '../../../modules/appraisal/ui/fixtures/stash-tab.json'
import characterFixture from '../../../modules/poeapi/ui/fixtures/character-selection.json'

/** Long enough to see `pricing…`, short enough not to be annoying. */
const PRICE_CHECK_DELAY_MS = 900

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * A credential that is usable, so the panel opens on the bag rather than on pairing.
 *
 * The name is nonsense on purpose: a preview that showed a plausible account name
 * would be a screenshot nobody could safely paste into an issue.
 */
const CREDENTIAL = {
  state: 'ok',
  account: 'Preview#0000',
  added_at: '2026-08-01T10:00:00Z',
  last_ok_at: '2026-08-12T09:00:00Z',
  rejected_at: null,
  stale: false,
  note: 'fixture data — no session, no requests',
  usable: true,
}

export class FixtureTransport implements Transport {
  readonly id = 'http' as const

  private readonly listeners = new ListenerSet()
  private readonly stateListeners = new Set<(state: TransportState) => void>()
  private _state: TransportState = 'idle'

  /** What `set_character` did, so the picker's own round trip is real. */
  private pinned: string | null = null

  get state(): TransportState {
    return this._state
  }

  async call<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    const answer = await this.answer(method, params)
    return answer as T
  }

  private async answer(method: string, params: Record<string, unknown>): Promise<unknown> {
    switch (method) {
      case '_server.meta':
        return { name: 'poedex', version: 'fixtures', methods: [], surface: 'deck-preview' }

      case 'credentials.status':
        return CREDENTIAL

      // The pairing screen's geometry is worth previewing — two big numbers and a
      // countdown at 300 px — so the window is simulated. The socket is not: there is
      // nothing listening and no credential can arrive.
      case 'credentials.pair_start':
      case 'credentials.pair_status':
        return {
          state: 'waiting',
          code: '000000',
          port: 7332,
          urls: ['http://192.168.0.0:7332'],
          expires_in: 180,
          attempts_left: 3,
          detail: 'fixture pairing window — nothing is listening',
        }
      case 'credentials.pair_cancel':
        return { state: 'cancelled', code: null, port: null, urls: [], expires_in: null, attempts_left: null, detail: null }

      case 'appraisal.appraise_bag':
        return bagFixture

      case 'appraisal.highlight':
        return { ...highlightFixture, uid: String(params.uid ?? '') }

      case 'appraisal.price_check':
        await delay(PRICE_CHECK_DELAY_MS)
        return { ...checkFixture, uid: String(params.uid ?? '') }

      case 'appraisal.stash_digest':
        return stashDigestFixture
      case 'appraisal.appraise_tab':
        return stashTabFixture

      case 'poeapi.character_choice':
        return this.characterSelection()
      case 'poeapi.set_character':
        this.pinned = (params.name as string | null) ?? null
        return this.characterSelection()

      case 'prices.refresh':
      case 'prices.status':
        return {}

      default:
        // Silence would be the wrong answer: an unanswered method is a screen the
        // preview cannot show, and the person iterating on it needs to know which.
        throw new TransportError(
          `${method} has no fixture. The preview answers from modules/*/ui/fixtures; ` +
            'run `poedex serve` in another terminal to drive this screen for real.',
          { kind: 'NotImplemented' },
        )
    }
  }

  private characterSelection(): unknown {
    const base = characterFixture as typeof characterFixture & { configured: string | null }
    if (!this.pinned) return { ...base, configured: null }
    const picked = base.characters.find((entry) => entry.name === this.pinned)
    return {
      ...base,
      configured: this.pinned,
      choice: {
        ...base.choice,
        name: this.pinned,
        // `setting`, which is what the backend calls a pin — not `configured`, which
        // is the *field* holding it. Writing the wrong one here put the literal word
        // `undefined` under the character's name in the panel, because `REASON` is
        // keyed by the enum; a nice demonstration of the tool and a bug in this file.
        source: 'setting',
        league: picked?.league ?? base.choice.league,
        class_name: picked?.class_name ?? base.choice.class_name,
        level: picked?.level ?? base.choice.level,
        played_last: base.choice.name === this.pinned ? null : base.choice.name,
      },
    }
  }

  on(pattern: string, listener: EventListener): Unsubscribe {
    return this.listeners.add(pattern, listener)
  }

  /** Nothing emits: there is no backend to emit anything. The state is honest. */
  connect(): void {
    this.setState('open')
  }

  close(): void {
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

/**
 * Is a real backend answering?
 *
 * One `_server.meta` through the same proxy every other call uses, so a `502` from
 * Vite (nothing on 7331) and a refused connection both come back the same way.
 */
export async function backendIsUp(signal?: AbortSignal): Promise<boolean> {
  try {
    const response = await fetch('/api/call/_server.meta', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
      ...(signal ? { signal } : {}),
    })
    if (!response.ok) return false
    const body = (await response.json()) as { ok?: boolean }
    return body?.ok === true
  } catch {
    return false
  }
}
