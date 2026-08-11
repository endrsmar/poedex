/**
 * The Decky panel: which screen you land on, and how you get back.
 *
 * These run **only** under `vitest.compact.config.ts`, because every claim in them
 * is a claim about the compact profile: that the stash screen is not offered at
 * 300 px, that the pairing screen is the root when there is no session, that the
 * shell says so when `@decky/ui` handed back nothing.
 *
 * What they cannot check is the input device. `onCancel` is Steam's **B** button and
 * nothing in jsdom presses it, so the back behaviour is driven through the shell's
 * own footer control and the *routing* is what is asserted. Whether B reaches this
 * `Focusable` at all is `docs/deck-checklist.md` item 5.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { clearClient, installClient } from '@poedex/core'
import type { PoedexClient } from '@poedex/core'
import { Panel } from './Panel'

type Listener = (event: { topic: string; payload: Record<string, unknown> }) => void

function harness(credential: Partial<Record<string, unknown>> = {}) {
  const listeners: { pattern: string; listener: Listener }[] = []
  const status = vi.fn(async () => ({
    state: 'ok',
    account: 'Exile',
    added_at: null,
    last_ok_at: null,
    rejected_at: null,
    stale: false,
    note: null,
    usable: true,
    ...credential,
  }))
  const client = {
    transport: {
      id: 'decky',
      state: 'open',
      call: vi.fn(async () => ({})),
      on: (pattern: string, listener: Listener) => {
        const entry = { pattern, listener }
        listeners.push(entry)
        return () => listeners.splice(listeners.indexOf(entry), 1)
      },
      connect: () => {},
      close: () => {},
      onStateChange: () => () => {},
    },
    meta: vi.fn(async () => ({})),
    credentials: {
      status,
      pairStart: vi.fn(async () => idle('waiting')),
      pairStatus: vi.fn(async () => idle('idle')),
      pairCancel: vi.fn(async () => idle('cancelled')),
    },
    appraisal: {
      bag: vi.fn(async () => {
        throw new Error('not in this test')
      }),
    },
    prices: { refresh: vi.fn(async () => ({})) },
  } as unknown as PoedexClient
  const emit = (topic: string, payload: Record<string, unknown> = {}) => {
    for (const entry of [...listeners]) {
      if (entry.pattern === topic) entry.listener({ topic, payload })
    }
  }
  return { client, emit, status }
}

function idle(state: string) {
  return {
    state,
    code: state === 'waiting' ? '123456' : null,
    port: 7332,
    urls: ['http://192.168.1.20:7332'],
    expires_in: 180,
    attempts_left: 3,
    detail: null,
  } as never
}

beforeEach(() => clearClient())
afterEach(() => {
  cleanup()
  clearClient()
})

describe('which screen the panel opens on', () => {
  it('opens on Pair when there is no usable session', async () => {
    const { client } = harness({ state: 'never_set', usable: false, account: null })
    installClient(client)
    render(<Panel />)
    // Not a tab somebody has to find: with no session there is nothing a bag screen
    // can say, so the question is the root.
    await waitFor(() => expect(screen.getByText('Pair with your PC')).toBeInTheDocument())
  })

  it('opens on the bag once a session is stored', async () => {
    const { client } = harness()
    installClient(client)
    render(<Panel />)
    await waitFor(() => expect(screen.getByRole('button', { name: /^Bag$/ })).toBeInTheDocument())
  })

  it('moves off the pairing screen the moment a credential arrives', async () => {
    const { client, emit, status } = harness({ state: 'never_set', usable: false, account: null })
    installClient(client)
    render(<Panel />)
    await waitFor(() => expect(screen.getByText('Pair with your PC')).toBeInTheDocument())

    // The root is derived rather than stored precisely so this works: a stored root
    // would leave the player looking at the pairing screen after pairing.
    status.mockResolvedValue({
      state: 'ok',
      account: 'Exile',
      added_at: null,
      last_ok_at: null,
      rejected_at: null,
      stale: false,
      note: null,
      usable: true,
    })
    emit('credential_changed', {})
    await waitFor(() => expect(screen.queryByText('Pair with your PC')).toBeNull())
  })
})

describe('the screen stack', () => {
  it('offers every compact screen and not the stash', async () => {
    const { client } = harness()
    installClient(client)
    render(<Panel />)
    await waitFor(() => expect(screen.getByRole('button', { name: /^Bag$/ })).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /^Pair$/ })).toBeInTheDocument()
    // Phase 10's decision, respected rather than reopened: `StashScreen` declares
    // `profiles: ['full']` because a 117-row tab list and a 24x24 quad do not fit at
    // 300 px, and the compact answer is a *digest*, which is a different screen.
    expect(screen.queryByRole('button', { name: /^Stash$/ })).toBeNull()
  })

  it('pushes a screen and pops back to the root', async () => {
    const { client } = harness()
    installClient(client)
    render(<Panel />)
    await waitFor(() => expect(screen.getByRole('button', { name: /^Pair$/ })).toBeInTheDocument())

    await userEvent.click(screen.getByRole('button', { name: /^Pair$/ }))
    await waitFor(() => expect(screen.getByText('what this does')).toBeInTheDocument())
    // The Back control only exists once there is something to go back to.
    const back = screen.getByRole('button', { name: /Back/ })
    await userEvent.click(back)
    await waitFor(() => expect(screen.queryByRole('button', { name: /Back/ })).toBeNull())
  })
})

describe('what the panel says when something is wrong', () => {
  it('shows a backend that failed to start, rather than an empty panel', async () => {
    const { client, emit } = harness()
    installClient(client)
    render(<Panel />)
    emit('backend.broken', { detail: 'py_modules/ is built for CPython 3.13' })
    await waitFor(() =>
      expect(screen.getByText(/py_modules\/ is built for CPython 3.13/)).toBeInTheDocument(),
    )
  })

  it('clears the failure when the backend comes up', async () => {
    const { client, emit } = harness()
    installClient(client)
    render(<Panel />)
    emit('backend.broken', { detail: 'still starting' })
    await waitFor(() => expect(screen.getByText(/still starting/)).toBeInTheDocument())
    emit('backend.ready', {})
    await waitFor(() => expect(screen.queryByText(/still starting/)).toBeNull())
  })
})
