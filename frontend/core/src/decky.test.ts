/**
 * `DeckyTransport`.
 *
 * The claim under test is the one the `Transport` interface exists to make: a screen
 * cannot tell which transport it got. So these assert the *same* behaviours
 * `http.test.ts` asserts of the other one — a resolved result, a `TransportError`
 * with a `kind` and a `retryAfter`, pattern-matched events — through Decky's very
 * different plumbing.
 *
 * The bridge is a fake, because `@decky/api` only exists inside the plugin host.
 * That is deliberate rather than a compromise: the bridge is three functions, and
 * its whole job is to be the seam between "what Decky's RPC looks like" and "what
 * this project needs", so a test on the far side of it is testing the thing that has
 * logic in it. What no test here can check is `callable()` and `addEventListener()`
 * themselves — `docs/deck-checklist.md` item 4.
 */

import { describe, expect, it, vi } from 'vitest'
import { DeckyTransport } from './decky'
import type { DeckyBridge, DeckyEnvelope } from './decky'
import { TransportError } from './transport'

function fakeBridge(overrides: Partial<DeckyBridge> = {}) {
  const handlers: ((message: unknown) => void)[] = []
  const bridge: DeckyBridge & { push(message: unknown): void; handlers: typeof handlers } = {
    call: vi.fn(async () => ({ ok: true, result: null }) as DeckyEnvelope),
    addEventListener: (handler) => {
      handlers.push(handler)
      return () => {
        const index = handlers.indexOf(handler)
        if (index >= 0) handlers.splice(index, 1)
      }
    },
    handlers,
    push(message: unknown) {
      for (const handler of [...handlers]) handler(message)
    },
    ...overrides,
  }
  return bridge
}

describe('construction', () => {
  it('refuses a missing bridge, and says where one comes from', () => {
    expect(() => new DeckyTransport(undefined as unknown as DeckyBridge)).toThrow(/@decky\/api/)
  })

  it('is open from the start — the backend outlived the panel', () => {
    // The backend is a separate OS process that survives the QAM closing, the game
    // launching and a Steam UI reload (SPEC §6.2). There is no connecting state to
    // report because there is genuinely nothing to wait for.
    expect(new DeckyTransport(fakeBridge()).state).toBe('open')
  })
})

describe('call', () => {
  it('unwraps a successful envelope', async () => {
    const bridge = fakeBridge({
      call: vi.fn(async () => ({ ok: true, result: { total: 5164 } })),
    })
    const transport = new DeckyTransport(bridge)
    await expect(transport.call('appraisal.appraise_bag', { character: 'Exile' })).resolves.toEqual({
      total: 5164,
    })
    expect(bridge.call).toHaveBeenCalledWith('appraisal.appraise_bag', { character: 'Exile' })
  })

  it('turns a failed envelope into a TransportError carrying the retry hint', async () => {
    // `retry_after` is why this is structured rather than a string: it is what runs
    // the countdown on the refresh control instead of a red box saying "try again"
    // while every attempt is refused.
    const bridge = fakeBridge({
      call: vi.fn(async () => ({
        ok: false,
        error: { kind: 'RateLimited', message: 'refused by the limiter', retry_after: 47 },
        status: 429,
      })),
    })
    const caught = await new DeckyTransport(bridge)
      .call('poeapi.get_items')
      .catch((error: unknown) => error)
    expect(caught).toBeInstanceOf(TransportError)
    const error = caught as TransportError
    expect(error.kind).toBe('RateLimited')
    expect(error.retryAfter).toBe(47)
    expect(error.restricted).toBe(true)
    expect(error.message).toBe('refused by the limiter')
  })

  it('distinguishes "the plugin is not there" from "the method failed"', async () => {
    const bridge = fakeBridge({
      call: vi.fn(async () => {
        throw new Error('websocket closed')
      }),
    })
    const caught = (await new DeckyTransport(bridge)
      .call('appraisal.appraise_bag')
      .catch((error: unknown) => error)) as TransportError
    expect(caught.kind).toBe('TransportUnavailable')
    expect(caught.restricted).toBe(false)
  })

  it('refuses a reply that is not an envelope rather than returning undefined', async () => {
    const bridge = fakeBridge({ call: vi.fn(async () => undefined) })
    const caught = (await new DeckyTransport(bridge)
      .call('appraisal.appraise_bag')
      .catch((error: unknown) => error)) as TransportError
    expect(caught.kind).toBe('ProtocolError')
  })
})

describe('events', () => {
  it('routes a decky.emit push to a matching listener', () => {
    const bridge = fakeBridge()
    const transport = new DeckyTransport(bridge)
    const seen: string[] = []
    transport.on('sync_complete', (event) => seen.push(event.topic))
    transport.on('appraisal.*', (event) => seen.push(event.topic))
    transport.connect()

    bridge.push({ topic: 'sync_complete', payload: { items: 42 }, source: 'poeapi', at: 1 })
    bridge.push({ topic: 'appraisal.complete', payload: {} })
    bridge.push({ topic: 'gamelog.zone_entered', payload: {} })

    expect(seen).toEqual(['sync_complete', 'appraisal.complete'])
  })

  it('ignores a push that is not an event, instead of throwing inside Decky', () => {
    const bridge = fakeBridge()
    const transport = new DeckyTransport(bridge)
    const seen: unknown[] = []
    transport.on('*', (event) => seen.push(event))
    transport.connect()
    bridge.push(null)
    bridge.push('a string')
    bridge.push({ payload: {} })
    expect(seen).toEqual([])
  })

  it('replays what the backend already emitted, for a panel that just mounted', async () => {
    // Panel content is unmounted whenever the QAM closes, so a screen mounting after
    // `sync_complete` fired would otherwise sit on a spinner until the next zone
    // change — which on a stash-only session may never come.
    const bridge = fakeBridge({
      latest: vi.fn(async () => ({
        sync_complete: { topic: 'sync_complete', payload: { items: 7 } },
      })),
    })
    const transport = new DeckyTransport(bridge)
    const seen: string[] = []
    transport.on('*', (event) => seen.push(event.topic))
    transport.connect()
    await Promise.resolve()
    await Promise.resolve()
    expect(seen).toEqual(['sync_complete'])
  })

  it('survives a replay that fails — freshness, never correctness', async () => {
    const bridge = fakeBridge({
      latest: vi.fn(async () => {
        throw new Error('backend still starting')
      }),
    })
    const transport = new DeckyTransport(bridge)
    transport.connect()
    await Promise.resolve()
    await Promise.resolve()
    bridge.push({ topic: 'sync_complete', payload: {} })
    // Nothing threw, and the live channel still works.
    expect(bridge.handlers.length).toBe(1)
  })

  it('attaches once and detaches on close', () => {
    const bridge = fakeBridge()
    const transport = new DeckyTransport(bridge)
    transport.connect()
    transport.connect()
    expect(bridge.handlers.length).toBe(1)
    transport.close()
    expect(bridge.handlers.length).toBe(0)
  })

  it('gives a no-op unsubscribe for state changes, so a shared chip does not crash', () => {
    const transport = new DeckyTransport(fakeBridge())
    expect(() => transport.onStateChange()()).not.toThrow()
  })
})
