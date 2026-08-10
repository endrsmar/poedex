/**
 * The stash screen, against fixtures the real backend wrote.
 *
 * `fixtures/stash-digest.json` and `fixtures/stash-tab.json` come out of
 * `scripts/make_ui_fixtures.py` through `StashDigest.to_json` and
 * `TabAppraisal.to_json`, and a Python test fails when they drift.
 *
 * Two claims carry this file, and both are about **not lying**:
 *
 * 1. **Nothing fetches a tab unless a row is pressed.** The digest is free; a tab is
 *    a request. A screen that read a tab on mount, on hover or on re-render would be
 *    the auto-crawl SPEC §6.6 forbids, wearing a UI.
 * 2. **An unread tab and a map tab never render as `0c`.** Both are *unknown*, and
 *    the total says so. This is the failure mode Phase 10 was warned about.
 *
 * It runs under **both** profiles, as every component test here does — the screen
 * declares `profiles: ['full']`, which is a judgement about where it belongs and
 * not a claim that it cannot render. The registration itself is asserted at the
 * bottom, so the judgement is checked rather than described.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { clearClient, installClient } from '@poedex/core'
import type {
  ItemHighlightPayload,
  PoedexClient,
  PriceCheckPayload,
  StashDigestPayload,
  TabAppraisalPayload,
} from '@poedex/core'
import { screensFor } from '@poedex/ui'
import { StashScreen } from './StashScreen'
import appraisalUI from './index'
import { describeAge, stashNote, tabRow as tabRowModel, tabVerdict } from './model'
import digestFixture from './fixtures/stash-digest.json'
import tabFixture from './fixtures/stash-tab.json'
import highlightFixture from './fixtures/item-highlight.json'
import checkFixture from './fixtures/price-check.json'

const DIGEST = digestFixture as unknown as StashDigestPayload
const TAB = tabFixture as unknown as TabAppraisalPayload
const HIGHLIGHT = highlightFixture as unknown as ItemHighlightPayload
const CHECK = checkFixture as unknown as PriceCheckPayload

interface Harness {
  client: PoedexClient
  stash: ReturnType<typeof vi.fn>
  tab: ReturnType<typeof vi.fn>
  highlight: ReturnType<typeof vi.fn>
  priceCheck: ReturnType<typeof vi.fn>
}

function harness(tabAnswer: TabAppraisalPayload = TAB): Harness {
  const stash = vi.fn(async () => DIGEST)
  const tab = vi.fn(async () => tabAnswer)
  const highlight = vi.fn(async (uid: string) => ({ ...HIGHLIGHT, uid }))
  const priceCheck = vi.fn(async (uid: string) => ({ ...CHECK, uid }))
  const client = {
    transport: {
      id: 'http',
      state: 'open',
      call: async () => undefined,
      on: () => () => {},
      connect: () => {},
      close: () => {},
      onStateChange: () => () => {},
    },
    appraisal: { stash, tab, highlight, priceCheck },
    prices: { refresh: vi.fn(async () => ({})) },
  } as unknown as PoedexClient
  return { client, stash, tab, highlight, priceCheck }
}

const section = (title: string) => screen.getByRole('group', { name: title })

/** The list row for a tab, by its name. Present at both densities. */
const tabRow = (name: string) =>
  within(section('tabs')).getByText(name).closest('[data-uid]') as HTMLElement

const rowsIn = (scope: HTMLElement) => Array.from(scope.querySelectorAll<HTMLElement>('[data-uid]'))

async function show(built: Harness = harness()) {
  installClient(built.client)
  const view = render(<StashScreen />)
  await waitFor(() => expect(screen.queryByText(/reading the tab list/i)).toBeNull())
  return { ...built, view }
}

beforeEach(() => clearClient())
afterEach(() => {
  cleanup()
  clearClient()
})

// -- what it costs to look ------------------------------------------------------

describe('the screen never spends a request by itself', () => {
  it('reads the digest on mount and no tab at all', async () => {
    const view = await show()
    expect(view.stash).toHaveBeenCalledTimes(1)
    // The whole rule, in one assertion. SPEC §6.6: never auto-crawl.
    expect(view.tab).not.toHaveBeenCalled()
  })

  it('reads a tab only when a row is pressed', async () => {
    const view = await show()
    await userEvent.click(within(section('tabs')).getByText('Gear'))
    await waitFor(() => expect(view.tab).toHaveBeenCalledTimes(1))
    expect(view.tab.mock.calls[0]?.[0]).toBe(1)
  })

  it('says what a full refresh would cost, before anyone asks for one', async () => {
    await show()
    expect(screen.getByText(/pause your inventory syncing/i)).toBeTruthy()
  })
})

// -- the two kinds of hole -------------------------------------------------------

describe('an unknown tab is never drawn as zero', () => {
  it('lists a tab nobody has read without a value', async () => {
    await show()
    const row = tabRow('Later')
    expect(row.textContent).toContain('not read')
    expect(row.textContent).not.toContain('0c')
  })

  it('says a map tab is not supported rather than empty', async () => {
    await show()
    const row = tabRow('M')
    expect(row.textContent).toContain('not supported')
    expect(row.textContent).not.toContain('0c')
  })

  it('marks the stash total as a floor and names both holes', async () => {
    await show()
    const note = stashNote(DIGEST)
    expect(note).toContain('never been read')
    expect(note).toContain('unknown, not zero')
    expect(note).toContain('cannot be read at all')
    expect(screen.getByText(new RegExp('This is a floor, not a value'))).toBeTruthy()
  })

  it('gives an unread tab and a map tab the unpriceable verdict, not trash', () => {
    const unread = DIGEST.tabs.find((tab) => !tab.known && tab.supported)!
    const maps = DIGEST.tabs.find((tab) => !tab.supported)!
    expect(tabVerdict(unread)).toBe('unpriceable')
    expect(tabVerdict(maps)).toBe('unpriceable')
    // ...and their price is `null`, which is what `PriceModel` uses for "no number",
    // rather than 0, which is a number.
    expect(tabRowModel(unread).price?.chaos).toBeNull()
    expect(tabRowModel(maps).price?.chaos).toBeNull()
  })
})

// -- layouts ---------------------------------------------------------------------

describe('tab layouts reach the screen', () => {
  it('shows the shape of every tab, quad included', async () => {
    await show()
    const quad = tabRow('Sext')
    expect(quad.textContent).toContain('24x24')
  })

  it('draws a 24x24 grid for a quad tab', async () => {
    const quadTab: TabAppraisalPayload = {
      ...TAB,
      tab: { ...TAB.tab, index: 2, name: 'Sext', kind: 'quad', cols: 24, rows: 24, grid: true },
    }
    await show(harness(quadTab))
    await userEvent.click(within(section('tabs')).getByText('Sext'))
    const grid = await screen.findByRole('grid', { name: /Sext layout/ })
    expect(grid.getAttribute('data-cols')).toBe('24')
  })

  it('lists a special tab instead of drawing an invented lattice', async () => {
    const currency: TabAppraisalPayload = {
      ...TAB,
      tab: { ...TAB.tab, index: 0, name: 'C', kind: 'currency', cols: null, rows: null, grid: false },
    }
    await show(harness(currency))
    await userEvent.click(within(section('tabs')).getByText('C'))
    await waitFor(() => expect(screen.getByText(/no grid/i)).toBeTruthy())
    expect(screen.queryByRole('grid', { name: /layout/ })).toBeNull()
  })
})

// -- the tab itself --------------------------------------------------------------

describe('an opened tab', () => {
  it('shows its rows under the same verdict blocks the bag uses', async () => {
    await show()
    await userEvent.click(within(section('tabs')).getByText('Gear'))
    await waitFor(() => expect(screen.getByText('Rift Shroud')).toBeTruthy())
    expect(screen.getByText('Veiled Scarab')).toBeTruthy()
  })

  it('keeps a removed item unpriceable rather than worthless', async () => {
    await show()
    await userEvent.click(within(section('tabs')).getByText('Gear'))
    const block = await waitFor(() => section('unpriceable'))
    expect(rowsIn(block).length).toBeGreaterThan(0)
    expect(block.textContent).toContain('Veiled Scarab')
  })

  it('says a map tab cannot be read instead of showing an empty tab', async () => {
    const unreadable: TabAppraisalPayload = {
      ...TAB,
      items: [],
      unsupported: 'map stash tabs are not supported yet: this endpoint returns no items',
      tab: { ...TAB.tab, index: 4, name: 'M', kind: 'map', supported: false },
    }
    await show(harness(unreadable))
    await userEvent.click(within(section('tabs')).getByText('M'))
    // Scoped to the alert, because the *row* also says "not supported": the point is
    // that opening the tab produces an explanation rather than an empty grid.
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('not supported yet')
    expect(alert.textContent).toContain('this endpoint returns no items')
    expect(screen.queryByText(/nothing in this tab/i)).toBeNull()
  })
})

// -- the price check, unforked ----------------------------------------------------

describe('the manual check is the bag’s, with a tab index', () => {
  it('offers the checkbox list for a highlighted stash rare', async () => {
    const view = await show()
    await userEvent.click(within(section('tabs')).getByText('Gear'))
    await waitFor(() => expect(screen.getByText('Rift Shroud')).toBeTruthy())
    await userEvent.click(screen.getByText('Rift Shroud'))
    await waitFor(() => expect(view.highlight).toHaveBeenCalled())
    // The tab index travels with the uid — the same call the bag makes, plus where
    // to look. Nothing about the question changed.
    expect(view.highlight.mock.calls[0]?.[2]).toBe(TAB.tab.index)
  })

  it('sends the tab index with the check itself', async () => {
    const view = await show()
    await userEvent.click(within(section('tabs')).getByText('Gear'))
    await waitFor(() => expect(screen.getByText('Rift Shroud')).toBeTruthy())
    await userEvent.click(screen.getByText('Rift Shroud'))
    await waitFor(() => expect(screen.getByText('Check price')).toBeTruthy())
    await userEvent.click(screen.getByText('Check price'))
    await waitFor(() => expect(view.priceCheck).toHaveBeenCalled())
    expect(view.priceCheck.mock.calls[0]?.[1]).toMatchObject({ tab_index: TAB.tab.index })
  })
})

// -- the registration -------------------------------------------------------------

describe('where this screen says it belongs', () => {
  it('declares full only, and the compact shell therefore never mounts it', () => {
    // §2.4's whole point: a screen may say where it makes sense. A 117-row tab list
    // and a 24x24 quad do not fit at 300 px, and research-notes §8 found that the
    // grid's justification does not transfer to the stash — you are standing at it.
    // The honest compact answer is a *digest*, which is a different screen.
    const full = screensFor(appraisalUI, 'full').map((entry) => entry.id)
    const compact = screensFor(appraisalUI, 'compact').map((entry) => entry.id)
    expect(full).toContain('stash')
    expect(compact).not.toContain('stash')
    // ...and the bag still belongs to both, which is what makes the exclusion a
    // judgement rather than a limitation of the kit.
    expect(compact).toContain('bag')
  })
})

// -- the model --------------------------------------------------------------------

describe('describeAge', () => {
  it('says never rather than 0 when nothing was ever read', () => {
    expect(describeAge(null)).toBe('never')
  })

  it('scales from seconds to days', () => {
    expect(describeAge(45)).toBe('45s ago')
    expect(describeAge(600)).toBe('10m ago')
    expect(describeAge(7200)).toBe('2h ago')
    expect(describeAge(86_400 * 3)).toBe('3d ago')
  })
})
