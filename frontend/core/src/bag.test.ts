/**
 * The six honest sync states.
 *
 * Each one gets a test because each one is a promise the surface makes about what
 * it knows, and the interesting failures are all of the form "state A quietly
 * rendered as state B". `unchanged` reported as `fresh` is the one that costs the
 * most trust and is the easiest to write by accident.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createBagStore } from './bag'
import { TransportError } from './transport'
import type { PoedexClient } from './methods'
import type { BagAppraisalPayload } from './types/generated'
import fixture from '../../../modules/appraisal/ui/fixtures/bag-appraisal.json'

const BAG = fixture as unknown as BagAppraisalPayload

function clientReturning(answers: (() => Promise<BagAppraisalPayload>)[]): {
  client: PoedexClient
  emit: (topic: string) => void
  calls: () => number
} {
  let index = 0
  const listeners: { pattern: string; fn: () => void }[] = []
  let calls = 0
  const client = {
    transport: {
      id: 'http',
      state: 'open',
      call: async () => undefined,
      on: (pattern: string, fn: () => void) => {
        listeners.push({ pattern, fn })
        return () => {}
      },
      connect: () => {},
      close: () => {},
      onStateChange: () => () => {},
    },
    appraisal: {
      bag: async () => {
        calls += 1
        const answer = answers[Math.min(index++, answers.length - 1)]!
        return answer()
      },
    },
  } as unknown as PoedexClient
  return {
    client,
    emit: (topic) => listeners.filter((l) => l.pattern === topic).forEach((l) => l.fn()),
    calls: () => calls,
  }
}

let now: Date

beforeEach(() => {
  now = new Date('2026-08-10T14:32:00Z')
})

describe('sync states', () => {
  it('is stale before anything has been fetched, and says so', () => {
    const { client } = clientReturning([async () => BAG])
    const store = createBagStore({ client, now: () => now })
    expect(store.get().sync.state).toBe('stale')
    expect(store.get().bag).toBeNull()
  })

  it('is syncing while a refresh is in flight, and keeps the bag on screen', async () => {
    let release: (() => void) | undefined
    const gate = new Promise<void>((resolve) => (release = resolve))
    const { client } = clientReturning([
      async () => BAG,
      async () => {
        await gate
        return BAG
      },
    ])
    const store = createBagStore({ client, now: () => now })
    await store.refresh()
    const first = store.get().bag

    const pending = store.refresh()
    expect(store.get().sync.state).toBe('syncing')
    // The grid dims elsewhere; it never blanks. The bag is still here.
    expect(store.get().bag).toBe(first)
    release!()
    await pending
  })

  it('is fresh when the answer differs', async () => {
    const { client } = clientReturning([async () => BAG])
    const store = createBagStore({ client, now: () => now })
    await store.refresh()
    expect(store.get().sync.state).toBe('fresh')
    expect(store.get().sync.at).toBe('2026-08-10T14:32:00.000Z')
  })

  it('is unchanged — not fresh — when the same bag comes back', async () => {
    const { client } = clientReturning([async () => BAG])
    const store = createBagStore({ client, now: () => now })
    await store.refresh()
    now = new Date('2026-08-10T14:41:00Z')
    await store.refresh()

    const { sync } = store.get()
    expect(sync.state).toBe('unchanged')
    // `at` still points at when the content last differed, which is what
    // "no change since 14:32" is built from. `checkedAt` moved.
    expect(sync.at).toBe('2026-08-10T14:32:00.000Z')
    expect(sync.checkedAt).toBe('2026-08-10T14:41:00.000Z')
  })

  it('notices a change in one row, and only the visible fields count', async () => {
    const moved: BagAppraisalPayload = {
      ...BAG,
      items: BAG.items.map((item, index) =>
        index === 0 ? { ...item, stack_size: item.stack_size + 1 } : item,
      ),
    }
    const bookkeeping: BagAppraisalPayload = { ...BAG, lookups: BAG.lookups + 5 }
    const { client } = clientReturning([async () => BAG, async () => bookkeeping, async () => moved])
    const store = createBagStore({ client, now: () => now })
    await store.refresh()
    await store.refresh()
    expect(store.get().sync.state).toBe('unchanged')
    await store.refresh()
    expect(store.get().sync.state).toBe('fresh')
  })

  it('is stale when the backend served cache without fetching', async () => {
    const { client } = clientReturning([async () => ({ ...BAG, stale: true })])
    const store = createBagStore({ client, now: () => now })
    await store.refresh()
    expect(store.get().sync.state).toBe('stale')
    expect(store.get().sync.detail).toMatch(/cache/)
    // The data is still shown. `stale` is a caveat, not a blank screen.
    expect(store.get().bag).not.toBeNull()
  })

  it('is restricted, with the limiter’s own number, when a request is refused', async () => {
    const { client } = clientReturning([
      async () => {
        throw new TransportError('rate limited: retry in 47s', {
          kind: 'RateLimited',
          retryAfter: 47,
        })
      },
    ])
    const store = createBagStore({ client, now: () => now })
    await store.refresh()
    expect(store.get().sync.state).toBe('restricted')
    expect(store.get().sync.retryAfter).toBe(47)
  })

  it('is error, with a reason, when anything else fails', async () => {
    const { client } = clientReturning([
      async () => {
        throw new TransportError('no league on the character', { kind: 'LeagueUnknownError' })
      },
    ])
    const store = createBagStore({ client, now: () => now })
    await store.refresh()
    expect(store.get().sync.state).toBe('error')
    expect(store.get().sync.detail).toBe('no league on the character')
    expect(store.get().sync.retryAfter).toBeNull()
  })

  it('keeps the last good bag through an error', async () => {
    const { client } = clientReturning([
      async () => BAG,
      async () => {
        throw new TransportError('gone', { kind: 'Unreachable' })
      },
    ])
    const store = createBagStore({ client, now: () => now })
    await store.refresh()
    await store.refresh()
    expect(store.get().sync.state).toBe('error')
    expect(store.get().bag?.total_chaos).toBe(BAG.total_chaos)
  })
})

describe('refresh behaviour', () => {
  it('coalesces concurrent refreshes into one request', async () => {
    const { client, calls } = clientReturning([async () => BAG])
    const store = createBagStore({ client, now: () => now })
    await Promise.all([store.refresh(), store.refresh(), store.refresh()])
    expect(calls()).toBe(1)
  })

  it('re-appraises when the backend says the bag may have changed', async () => {
    const { client, emit, calls } = clientReturning([async () => BAG])
    const store = createBagStore({ client, now: () => now })
    store.listen()
    emit('sync_complete')
    await vi.waitFor(() => expect(calls()).toBe(1))
  })

  it('notifies subscribers on every transition', async () => {
    const { client } = clientReturning([async () => BAG])
    const store = createBagStore({ client, now: () => now })
    const seen: string[] = []
    store.subscribe(() => seen.push(store.get().sync.state))
    await store.refresh()
    expect(seen).toEqual(['syncing', 'fresh'])
  })
})
