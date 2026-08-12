/**
 * A geometric focus resolver — Steam's navigation model, reimplemented over the DOM.
 *
 * ## Why this is not "tab order"
 *
 * Steam's gamepad UI does **not** navigate an ordered list. It resolves a direction
 * against the live rectangles of whatever is focusable on screen, which is what makes
 * `CLAUDE.md`'s claim — *"2D grid navigation is free: lay out a CSS grid of
 * `Focusable` cells and it works"* — true. A preview that walked the DOM in document
 * order would agree with the Deck exactly as often as the DOM happens to match the
 * layout, pass on the bag grid (row-major, so they agree), and quietly disagree on
 * every screen where they do not. That is worse than no preview: it would be a green
 * light for a panel that is broken on hardware, which is the failure mode this project
 * has already been bitten by.
 *
 * So this file is deliberately the one piece of the Deck preview with real tests. It
 * has no DOM dependencies in its scoring half: {@link resolveDirection} takes
 * rectangles and returns an index, so a test can construct a layout where document
 * order and geometry disagree and assert which one wins.
 *
 * ## The model, stated so it can be checked against hardware
 *
 * 1. **Candidates are the focusable *leaves*.** A `Focusable` that contains other
 *    focusables is a *scope*, not a target — `flow-children` is how Steam is told to
 *    delegate through it. {@link collectFocusables} therefore drops any element that
 *    is an ancestor of another candidate. Without that rule the Decky panel's root
 *    `Focusable` (the one that handles **B**) is a 300 × 533 rectangle sitting on top
 *    of every cell, and "down" lands on the whole panel.
 * 2. **A candidate must lie beyond the origin's trailing edge** in the direction
 *    pressed — `left >= origin.right` for *right*, and so on. Nothing else qualifies,
 *    so nothing "in front of you" can be reached by pressing away from it.
 * 3. **Score is distance along the axis plus a penalty for being off it**, and *off
 *    it* is measured from the origin's own centre line: a candidate the line passes
 *    through costs nothing, and anything else costs what it misses by. That is what
 *    makes a row of a grid feel like a row — pressing right walks the row instead of
 *    drifting into the one below, whose cells *touch* it and would score identically
 *    under a plain overlap test. It is also what lets a 24 px cell drop onto a
 *    280 px-wide item row without the row's width counting against it.
 * 4. **An edge refuses.** No candidate in that direction means focus does not move.
 *    Steam does not wrap a grid around, and a preview that wrapped would teach a
 *    layout habit the hardware punishes.
 *
 * ## What it cannot tell you
 *
 * This is a *model of* Steam's focus system written from its observable behaviour,
 * not Steam's implementation. It does not know about `flow-children="grid"` hints,
 * focus memory when returning to a scope, virtual-keyboard interception, or whatever
 * Valve does about elements that overlap. Where it disagrees with hardware, hardware
 * is right — `docs/deck-checklist.md` item 4 is still the thing that settles it.
 */

export type Direction = 'up' | 'down' | 'left' | 'right'

/** The part of a `DOMRect` any of this needs. */
export interface Rect {
  left: number
  top: number
  right: number
  bottom: number
}

/**
 * How much a candidate is punished for being off the axis, per pixel it misses by.
 *
 * Above 1 so that alignment beats proximity: in a 12-column grid the cell directly
 * below the origin must win over the cell one column across and half a row nearer,
 * and skipping a hole in a row must reach the next cell *in that row* rather than
 * the diagonal one which is closer as the crow flies.
 *
 * Nothing here is a measurement of Steam's own constant — it is the weight that
 * reproduces Steam's *observable* behaviour on the layouts this panel has, which is
 * the most this file can honestly claim.
 */
const CROSS_PENALTY = 3

/** Sub-pixel slack, so a grid gap of exactly 0 still counts as "beyond". */
const EPSILON = 0.5

interface Axis {
  /** How far into the direction a rectangle's leading edge is. */
  lead: (rect: Rect) => number
  /** The origin's own edge a candidate has to clear. */
  trail: (rect: Rect) => number
  /** The cross-axis span, as `[start, end]`. */
  cross: (rect: Rect) => [number, number]
}

const AXES: Record<Direction, Axis> = {
  right: {
    lead: (r) => r.left,
    trail: (r) => r.right,
    cross: (r) => [r.top, r.bottom],
  },
  left: {
    lead: (r) => -r.right,
    trail: (r) => -r.left,
    cross: (r) => [r.top, r.bottom],
  },
  down: {
    lead: (r) => r.top,
    trail: (r) => r.bottom,
    cross: (r) => [r.left, r.right],
  },
  up: {
    lead: (r) => -r.bottom,
    trail: (r) => -r.top,
    cross: (r) => [r.left, r.right],
  },
}

/**
 * How far a span sits from a point — 0 when the point is inside it.
 *
 * The point is the origin's cross-axis centre, which is the line the D-pad is
 * conceptually travelling along. Measuring from the centre rather than from the
 * origin's *edges* is what separates the cell below from the cell diagonally below:
 * in a grid with no gutters those two touch, so an overlap test scores them equal and
 * the tie is then broken by document order — which is the entire bug this file exists
 * to avoid.
 */
function missBy(point: number, span: readonly [number, number]): number {
  if (point < span[0]) return span[0] - point
  if (point > span[1]) return point - span[1]
  return 0
}

/**
 * Which candidate the D-pad moves to, or `null` for "focus stays where it is".
 *
 * The origin may appear in `candidates`; it is skipped by identity, and by geometry
 * anyway. Ties break towards the better-aligned candidate and then towards the lower
 * index, so a layout with genuinely ambiguous geometry is at least deterministic.
 */
export function resolveDirection(
  from: Rect,
  candidates: readonly Rect[],
  direction: Direction,
): number | null {
  const axis = AXES[direction]
  const trail = axis.trail(from)
  const [crossStart, crossEnd] = axis.cross(from)
  const centre = (crossStart + crossEnd) / 2

  let best: number | null = null
  let bestScore = Infinity
  let bestMiss = Infinity

  for (let index = 0; index < candidates.length; index += 1) {
    const candidate = candidates[index]
    if (!candidate || candidate === from) continue

    const advance = axis.lead(candidate) - trail
    if (advance < -EPSILON) continue

    const miss = missBy(centre, axis.cross(candidate))
    const score = Math.max(advance, 0) + miss * CROSS_PENALTY

    if (score < bestScore || (score === bestScore && miss < bestMiss)) {
      best = index
      bestScore = score
      bestMiss = miss
    }
  }

  return best
}

// ------------------------------------------------------------------- DOM ----

/** Injected so a jsdom test can state a layout the engine then navigates. */
export type Measure = (element: HTMLElement) => Rect

const defaultMeasure: Measure = (element) => element.getBoundingClientRect()

/**
 * Everything `@decky/ui`'s components make focusable.
 *
 * `Focusable` renders a `div` with `tabIndex` and the button primitives render real
 * `button`s, so this covers the whole compact profile. Anything else with a
 * `tabindex` is included too, because Steam would find it as well.
 */
const CANDIDATE_SELECTOR = 'button, [tabindex]'

/**
 * Is this element something focus could land on at all?
 *
 * Attribute-driven rather than style-driven on purpose: jsdom computes no layout, so
 * a rule like "it is not `display: none`" would be untestable here and would then be
 * the one rule nobody had checked. Zero area is handled by {@link collectFocusables}
 * through the injected measure, which a test can state.
 */
export function isFocusCandidate(element: HTMLElement): boolean {
  if (element.hasAttribute('disabled')) return false
  if (element.getAttribute('tabindex') === '-1') return false
  if (element.hasAttribute('inert')) return false
  if (element.closest('[aria-hidden="true"], [hidden], [inert]')) return false
  return true
}

export interface FocusTarget {
  element: HTMLElement
  rect: Rect
}

/**
 * The focusable leaves under `root`, with their rectangles, in document order.
 *
 * Document order is kept only as a tie-break and as the starting point when nothing
 * is focused yet; every actual movement is decided by {@link resolveDirection}.
 */
export function collectFocusables(root: ParentNode, measure: Measure = defaultMeasure): FocusTarget[] {
  const found: HTMLElement[] = []
  for (const node of Array.from(root.querySelectorAll<HTMLElement>(CANDIDATE_SELECTOR))) {
    if (isFocusCandidate(node)) found.push(node)
  }

  // Rule 1: a focusable that contains another focusable is a scope, not a target.
  const leaves = found.filter((node) => !found.some((other) => other !== node && node.contains(other)))

  const targets: FocusTarget[] = []
  for (const element of leaves) {
    const rect = measure(element)
    if (rect.right - rect.left <= 0 || rect.bottom - rect.top <= 0) continue
    targets.push({ element, rect })
  }
  return targets
}

/** What a press did, for a preview that has to be able to say "nothing". */
export type PressResult = 'moved' | 'refused' | 'activated' | 'cancelled' | 'unbound'

export interface GamepadFocusOptions {
  measure?: Measure
  /** Called whenever focus lands somewhere new. */
  onChange?: (element: HTMLElement | null) => void
}

/**
 * The D-pad, over a live subtree.
 *
 * Deliberately not a React component and not a hook: it is driven by a preview shell,
 * by a test, or by an on-screen button pad, and none of those should have to mount
 * anything. It holds no focus state of its own — `document.activeElement` is the
 * state, so a mouse click and a D-pad press cannot disagree.
 */
export class GamepadFocus {
  private readonly measure: Measure
  private readonly onChange: ((element: HTMLElement | null) => void) | undefined

  constructor(
    private readonly root: HTMLElement,
    options: GamepadFocusOptions = {},
  ) {
    this.measure = options.measure ?? defaultMeasure
    this.onChange = options.onChange
  }

  targets(): FocusTarget[] {
    return collectFocusables(this.root, this.measure)
  }

  /** The focused element, but only if it is one of ours. */
  get current(): HTMLElement | null {
    const active = this.root.ownerDocument.activeElement
    if (!(active instanceof HTMLElement)) return null
    return this.root.contains(active) ? active : null
  }

  /**
   * Move, or refuse.
   *
   * With nothing focused this focuses the first target rather than refusing, which is
   * what Steam does when the panel opens: something is always focused in the QAM.
   */
  move(direction: Direction): PressResult {
    const targets = this.targets()
    if (targets.length === 0) return 'refused'

    const active = this.current
    const originIndex = targets.findIndex((target) => target.element === active)
    if (originIndex < 0) {
      this.focus(targets[0]!.element)
      return 'moved'
    }

    const rects = targets.map((target) => target.rect)
    const next = resolveDirection(rects[originIndex]!, rects, direction)
    if (next === null) return 'refused'
    this.focus(targets[next]!.element)
    return 'moved'
  }

  /**
   * **A.** A click, because that is what the profile's `onActivate` is wired to.
   *
   * Not a synthetic `Enter` keydown: the compact profile maps `onActivate` to both,
   * and firing both would tick a checkbox twice.
   */
  activate(): PressResult {
    const active = this.current
    if (!active) return 'refused'
    active.click()
    return 'activated'
  }

  /**
   * **B.** An `Escape` keydown from the focused element, left to bubble.
   *
   * This is the one place the emulation is structurally faithful almost for free:
   * Steam's `onCancel` bubbles through the tree of `Focusable`s and a handler stops
   * it by calling `stopPropagation`, which is exactly what a DOM keydown does and
   * exactly what `surfaces/decky/src/Panel.tsx` relies on.
   *
   * `handled` is how the preview can say what the QAM would do next. The double marks
   * a consumed cancel by calling `preventDefault` on the native event when the React
   * handler stopped propagation; an unconsumed **B** at the root is the case where
   * the real QAM closes, which is checklist item 5's second half.
   */
  cancel(): { result: PressResult; handled: boolean } {
    const from = this.current ?? this.root
    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    from.dispatchEvent(event)
    return { result: 'cancelled', handled: event.defaultPrevented }
  }

  private focus(element: HTMLElement): void {
    element.focus()
    this.onChange?.(element)
  }
}
