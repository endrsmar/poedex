/**
 * The focus resolver, against layouts where document order is the wrong answer.
 *
 * This is the only part of the Deck preview with tests, and it is the part that would
 * be worth nothing without them: a preview whose focus order is DOM order passes on
 * the one screen where the two agree and lies about every other, which is a green
 * light for a panel that is broken on hardware.
 *
 * jsdom computes no layout, so every rectangle here is *stated* — `data-rect` on the
 * element and a `measure` that reads it. That is a feature of these tests rather than
 * a limitation: the layouts below are the ones worth asserting about (a grid whose
 * DOM order is column-major, a row with a hole in it, a container wrapping cells) and
 * a real browser would only be able to produce them by way of a stylesheet nobody
 * would then be able to read.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  GamepadFocus,
  collectFocusables,
  isFocusCandidate,
  resolveDirection,
  type Rect,
} from './gamepad-focus'

/** `data-rect="left,top,width,height"`. */
function measure(element: HTMLElement): Rect {
  const [left = 0, top = 0, width = 0, height = 0] = (element.dataset.rect ?? '')
    .split(',')
    .map(Number)
  return { left, top, right: left + width, bottom: top + height }
}

function rect(left: number, top: number, width: number, height: number): Rect {
  return { left, top, right: left + width, bottom: top + height }
}

/**
 * A 3 × 2 grid of 24 px cells, **written into the DOM column by column**.
 *
 * Document order is `c0r0, c0r1, c1r0, c1r1, c2r0, c2r1`; the layout reads
 * `c0r0, c1r0, c2r0` across the top. Nothing about the two orders agrees except the
 * first cell, so a resolver that walked the DOM would answer `c0r1` to "right".
 */
function columnMajorGrid(): HTMLElement {
  const root = document.createElement('div')
  for (let column = 0; column < 3; column += 1) {
    for (let row = 0; row < 2; row += 1) {
      const cell = document.createElement('div')
      cell.tabIndex = 0
      cell.id = `c${column}r${row}`
      cell.dataset.rect = `${column * 24},${row * 24},24,24`
      root.append(cell)
    }
  }
  document.body.append(root)
  return root
}

beforeEach(() => {
  document.body.innerHTML = ''
})

describe('resolveDirection', () => {
  // A row of three cells, given to the resolver in the order 2, 0, 1 — so an
  // implementation that returned "the next index" would be right by accident about
  // one of these three and wrong about the rest.
  const shuffled = [rect(48, 0, 24, 24), rect(0, 0, 24, 24), rect(24, 0, 24, 24)]

  it('answers with the geometric neighbour, not the next index', () => {
    expect(resolveDirection(shuffled[1]!, shuffled, 'right')).toBe(2)
    expect(resolveDirection(shuffled[2]!, shuffled, 'right')).toBe(0)
    expect(resolveDirection(shuffled[0]!, shuffled, 'left')).toBe(2)
  })

  it('refuses at an edge rather than wrapping', () => {
    // Steam does not wrap a grid, and a preview that wrapped would teach a layout
    // habit the hardware punishes. `null` is "focus stays where it is".
    expect(resolveDirection(shuffled[0]!, shuffled, 'right')).toBeNull()
    expect(resolveDirection(shuffled[1]!, shuffled, 'left')).toBeNull()
    expect(resolveDirection(shuffled[1]!, shuffled, 'up')).toBeNull()
  })

  it('prefers the aligned candidate over a nearer misaligned one', () => {
    // The bag grid's whole feel: pressing down from a cell lands under it, not on
    // the cell one column across that happens to start two pixels sooner.
    const origin = rect(100, 0, 24, 24)
    const candidates = [
      rect(148, 22, 24, 24), // two columns across, and *nearer* down the axis
      rect(100, 26, 24, 24), // directly below
    ]
    expect(resolveDirection(origin, candidates, 'down')).toBe(1)
  })

  it('walks the row rather than drifting into the next one', () => {
    const origin = rect(0, 0, 24, 24)
    const candidates = [
      rect(24, 24, 24, 24), // diagonally down-right, 24 px away on both axes
      rect(24, 0, 24, 24), // the next cell in the row
    ]
    expect(resolveDirection(origin, candidates, 'right')).toBe(1)
  })

  it('ignores everything that is not in the direction pressed', () => {
    const origin = rect(100, 100, 24, 24)
    // One pixel of overlap on the leading edge is not "beyond" it.
    expect(resolveDirection(origin, [rect(90, 100, 24, 24)], 'right')).toBeNull()
    expect(resolveDirection(origin, [rect(124, 100, 24, 24)], 'right')).toBe(0)
  })

  it('reaches a candidate whose row is a different height', () => {
    // A grid cell above a full-width item row: they share no cross-axis alignment
    // beyond overlap, and "down" still has to find it.
    const cell = rect(48, 0, 24, 24)
    const row = rect(0, 30, 280, 32)
    expect(resolveDirection(cell, [row], 'down')).toBe(0)
  })
})

describe('collectFocusables', () => {
  it('skips a cell that is not focusable, and moves past it', () => {
    const root = columnMajorGrid()
    const hole = root.querySelector<HTMLElement>('#c1r0')!
    hole.setAttribute('tabindex', '-1')

    const targets = collectFocusables(root, measure)
    expect(targets.map((target) => target.element.id)).not.toContain('c1r0')

    // ...and the gap does not stop navigation: right from c0r0 crosses it to c2r0.
    const rects = targets.map((target) => target.rect)
    const from = targets.findIndex((target) => target.element.id === 'c0r0')
    const next = resolveDirection(rects[from]!, rects, 'right')
    expect(targets[next!]!.element.id).toBe('c2r0')
  })

  it.each([
    ['disabled', (el: HTMLElement) => el.setAttribute('disabled', '')],
    ['aria-hidden', (el: HTMLElement) => el.setAttribute('aria-hidden', 'true')],
    ['inert', (el: HTMLElement) => el.setAttribute('inert', '')],
    ['zero-sized', (el: HTMLElement) => (el.dataset.rect = '24,0,0,0')],
  ])('drops a %s candidate', (_name, spoil) => {
    const root = columnMajorGrid()
    spoil(root.querySelector<HTMLElement>('#c1r0')!)
    const ids = collectFocusables(root, measure).map((target) => target.element.id)
    expect(ids).not.toContain('c1r0')
    expect(ids).toContain('c1r1')
  })

  it('treats a focusable that wraps focusables as a scope, not a target', () => {
    // The Decky panel's root `Focusable` — the one that handles B — is a 300 × 533
    // rectangle over every cell. Without this rule, "down" lands on the whole panel.
    const root = document.createElement('div')
    root.innerHTML = `
      <div tabindex="0" id="panel" data-rect="0,0,300,533">
        <div tabindex="0" id="a" data-rect="0,0,24,24"></div>
        <div tabindex="0" id="b" data-rect="0,24,24,24"></div>
      </div>`
    document.body.append(root)
    expect(collectFocusables(root, measure).map((t) => t.element.id)).toEqual(['a', 'b'])
  })

  it('counts a hidden ancestor, not just the element itself', () => {
    const root = document.createElement('div')
    root.innerHTML = `
      <div hidden><button id="off" data-rect="0,0,24,24"></button></div>
      <button id="on" data-rect="0,24,24,24"></button>`
    document.body.append(root)
    expect(collectFocusables(root, measure).map((t) => t.element.id)).toEqual(['on'])
  })

  it('is the same judgement `isFocusCandidate` makes on its own', () => {
    const button = document.createElement('button')
    expect(isFocusCandidate(button)).toBe(true)
    button.setAttribute('disabled', '')
    expect(isFocusCandidate(button)).toBe(false)
  })
})

describe('GamepadFocus', () => {
  it('focuses the first target when nothing is focused, then navigates', () => {
    const root = columnMajorGrid()
    const pad = new GamepadFocus(root, { measure })

    expect(pad.current).toBeNull()
    expect(pad.move('right')).toBe('moved')
    expect(document.activeElement?.id).toBe('c0r0')

    expect(pad.move('right')).toBe('moved')
    expect(document.activeElement?.id).toBe('c1r0')

    expect(pad.move('down')).toBe('moved')
    expect(document.activeElement?.id).toBe('c1r1')

    expect(pad.move('down')).toBe('refused')
    expect(document.activeElement?.id).toBe('c1r1')
  })

  it('reports refusal without moving focus', () => {
    const root = columnMajorGrid()
    const pad = new GamepadFocus(root, { measure })
    pad.move('down') // takes the initial focus
    expect(pad.move('left')).toBe('refused')
    expect(document.activeElement?.id).toBe('c0r0')
  })

  it('clicks the focused element on A, and only once', () => {
    const root = columnMajorGrid()
    const pressed = vi.fn()
    root.querySelector('#c0r0')!.addEventListener('click', pressed)
    const pad = new GamepadFocus(root, { measure })

    expect(pad.activate()).toBe('refused')
    pad.move('right')
    expect(pad.activate()).toBe('activated')
    expect(pressed).toHaveBeenCalledTimes(1)
  })

  it('bubbles B, and says whether anything consumed it', () => {
    const root = columnMajorGrid()
    const pad = new GamepadFocus(root, { measure })
    pad.move('right')

    // Nothing is listening: this is the root case, where the real QAM closes.
    expect(pad.cancel().handled).toBe(false)

    // A handler above the focused cell — the shape `Panel.tsx` has.
    root.addEventListener('keydown', (event) => {
      if ((event as KeyboardEvent).key === 'Escape') event.preventDefault()
    })
    expect(pad.cancel().handled).toBe(true)
  })
})
