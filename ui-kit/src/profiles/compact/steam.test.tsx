/**
 * The `@decky/ui` swap, and the guard around it.
 *
 * Two questions, and they are the two this phase can actually answer without a Deck:
 *
 * 1. **Does the compact profile delegate?** Not "does it still render the right
 *    words" — the 165-test suite already answers that through both profiles — but
 *    does a grid cell really become a `Focusable`, with `noFocusRing` on it, and does
 *    a check row really get `onActivate`. Those are the props the hardware behaviour
 *    hangs off, and asserting them is the difference between a swap and a claim.
 *
 * 2. **What happens when `@decky/ui` hands back nothing?** It finds Steam's
 *    components by regex over minified code, so a client update can make one come
 *    back `undefined`, and `<undefined>` in a React tree is a **white QAM panel**.
 *    The answer has to be "degraded and legible, and it says which component is
 *    missing" — which is what the second half of this file pins down.
 *
 * Neither question is *"is real `@decky/ui` correct here"*. That needs hardware and
 * is item 2 of `docs/deck-checklist.md`. Nothing in this file should ever be cited
 * as evidence about a real Deck.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CheckList, ItemGrid, ItemRow, Section, Stepper } from './index'
import { MISSING_STEAM_COMPONENTS, steamComponentWarning } from './steam'
import type { GridCellModel, ItemRowModel } from '../../contracts'

const cell = (over: Partial<GridCellModel> = {}): GridCellModel => ({
  uid: 'c1',
  glyph: 'DO',
  label: 'Divine Orb x3',
  verdict: 'keep',
  rarity: 'currency',
  x: 0,
  y: 0,
  ...over,
})

const row: ItemRowModel = {
  uid: 'u1',
  name: 'Divine Orb',
  rarity: 'currency',
  verdict: 'keep',
  price: { chaos: 642, provenance: 'bulk' },
}

afterEach(cleanup)

describe('the compact primitives delegate to @decky/ui', () => {
  it('draws grid cells as Focusable, with the ring suppressed', () => {
    // SPEC §6.1 measured Steam's default focus ring thick enough to merge adjacent
    // 22 px cells. On a grid whose whole job is to say *which slot*, that is the ring
    // erasing the answer, so `noFocusRing` is load-bearing rather than cosmetic.
    render(<ItemGrid cells={[cell()]} cols={12} rows={5} />)
    const node = document.querySelector('[data-uid="c1"]')
    expect(node?.getAttribute('data-decky')).toBe('Focusable')
    expect(node?.getAttribute('data-no-focus-ring')).toBe('true')
    expect(node?.getAttribute('data-focus-class')).toBe('poedex-focus')
  })

  it('drives selection from gamepad focus, which is how detail fits in 268 px', async () => {
    const onSelect = vi.fn()
    render(<ItemGrid cells={[cell()]} cols={12} rows={5} onSelect={onSelect} />)
    const node = document.querySelector('[data-uid="c1"]') as HTMLElement
    await act(async () => node.focus())
    expect(onSelect).toHaveBeenCalledWith('c1')
    // Exactly once. A cell that also took `onActivate` would fire twice for one
    // D-pad landing plus a press, and the stash tab list spends a request per select.
    expect(node.getAttribute('data-on-activate')).toBeNull()
    expect(onSelect).toHaveBeenCalledTimes(1)
  })

  it('draws item rows as Focusable too', () => {
    render(<ItemRow item={row} onSelect={() => {}} />)
    expect(document.querySelector('[data-uid="u1"]')?.getAttribute('data-decky')).toBe('Focusable')
  })

  it('gives a check row onActivate, so A ticks the box', async () => {
    const onToggle = vi.fn()
    render(
      <CheckList
        options={[{ id: 'm0', label: '+95 to maximum Life', badge: 'T4 of 10' }]}
        selected={[]}
        onToggle={onToggle}
      />,
    )
    const node = document.querySelector('[data-check-id="m0"]') as HTMLElement
    expect(node.getAttribute('data-decky')).toBe('Focusable')
    expect(node.getAttribute('data-on-activate')).toBe('true')
    await act(async () => node.focus())
    await userEvent.keyboard('{Enter}')
    expect(onToggle).toHaveBeenCalledWith('m0')
  })

  it('does not make a disabled check row activatable', () => {
    render(
      <CheckList
        options={[
          { id: 'm0', label: '+15% increased Light Radius', disabled: true, disabledReason: 'no trade filter' },
        ]}
        selected={[]}
        onToggle={() => {}}
      />,
    )
    expect(document.querySelector('[data-check-id="m0"]')?.getAttribute('data-on-activate')).toBeNull()
  })

  it('passes a section title to PanelSection', () => {
    render(
      <Section title="bag layout" meta="18 rows">
        <span>a row</span>
      </Section>,
    )
    expect(document.querySelector('[data-decky="PanelSection"]')?.getAttribute('data-panel-title')).toBe(
      'bag layout',
    )
  })

  it('offers "show all" as a ButtonItem — a row-sized target, not a word', async () => {
    const onShowAll = vi.fn()
    render(
      <Section title="trash" limit={1} onShowAll={onShowAll}>
        <span>one</span>
        <span>two</span>
        <span>three</span>
      </Section>,
    )
    const button = document.querySelector('[data-decky="ButtonItem"]') as HTMLElement
    expect(button).toBeTruthy()
    await userEvent.click(button)
    expect(onShowAll).toHaveBeenCalled()
  })

  it('lays a stepper out as a Field with a button either side of the value', () => {
    render(<Stepper label="open prefixes" value={2} onChange={() => {}} />)
    expect(document.querySelector('[data-decky="Field"]')).toBeTruthy()
    expect(document.querySelectorAll('[data-decky="DialogButton"]').length).toBe(2)
  })

  it('reports nothing missing when every component arrived', () => {
    expect(MISSING_STEAM_COMPONENTS).toEqual([])
    expect(steamComponentWarning()).toBeNull()
  })
})

/**
 * Every name spelled out and set to `undefined`, which is exactly the shape the real
 * failure has: `@decky/rollup` externalises `@decky/ui` to the global `DFL`, so a
 * component its regex no longer finds is a property read that yields `undefined` —
 * never an import error. (Vitest refuses to serve a name a mock factory omits, so
 * an empty object would test the wrong thing anyway.)
 */
const NOTHING_RESOLVED = {
  Focusable: undefined,
  PanelSection: undefined,
  PanelSectionRow: undefined,
  ScrollPanel: undefined,
  DialogButton: undefined,
  ButtonItem: undefined,
  Field: undefined,
}

/**
 * The degraded path.
 *
 * `vi.doMock` rather than `vi.mock` because these need the module graph rebuilt
 * *after* the mock is in place: `steam.tsx` resolves every component once, at load,
 * so that the shell can show the miss before a screen has had a chance to fail.
 */
describe('when Steam does not supply a component', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  afterEach(() => {
    vi.doUnmock('@decky/ui')
    vi.resetModules()
  })

  it('names every missing component instead of white-screening', async () => {
    vi.doMock('@decky/ui', () => NOTHING_RESOLVED)
    const steam = await import('./steam')
    expect(steam.MISSING_STEAM_COMPONENTS).toEqual([
      'Focusable',
      'PanelSection',
      'PanelSectionRow',
      'ScrollPanel',
      'DialogButton',
      'ButtonItem',
      'Field',
    ])
    const warning = steam.steamComponentWarning()
    expect(warning).toContain('Focusable')
    expect(warning).toContain('Steam client update')
  })

  it('still draws the grid, and it is still operable', async () => {
    vi.doMock('@decky/ui', () => NOTHING_RESOLVED)
    const compact = await import('./index')
    const onSelect = vi.fn()
    render(
      <compact.ItemGrid cells={[cell({ label: 'Divine Orb x3' })]} cols={12} rows={5} onSelect={onSelect} />,
    )
    // The content is the assertion. A blank panel is the failure this guard exists
    // for, so "something visible with the right label in it" is the bar.
    const node = screen.getByLabelText('Divine Orb x3, keep')
    expect(node.getAttribute('data-decky')).toBeNull()
    await act(async () => (node as HTMLElement).focus())
    expect(onSelect).toHaveBeenCalledWith('c1')
  })

  it('degrades one component without taking the others with it', async () => {
    const real = await vi.importActual<Record<string, unknown>>('@decky/ui')
    vi.doMock('@decky/ui', () => ({ ...real, Focusable: undefined }))
    const steam = await import('./steam')
    expect(steam.MISSING_STEAM_COMPONENTS).toEqual(['Focusable'])

    const compact = await import('./index')
    render(
      <compact.Section title="bag layout">
        <span>a row</span>
      </compact.Section>,
    )
    // `PanelSection` still came from `@decky/ui`; only `Focusable` fell back.
    expect(document.querySelector('[data-decky="PanelSection"]')).toBeTruthy()
  })
})
