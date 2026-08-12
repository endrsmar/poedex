/**
 * The Deck preview shell: the real QAM panel, at the Deck's real geometry, in a browser.
 *
 * ## What is real here and what is not
 *
 * **Real:** the panel. `<Panel/>` is `surfaces/decky/src/Panel.tsx`, mounted through the
 * same module registry the plugin uses, rendering the `compact` profile — this build
 * sets `resolve.conditions: ['poedex-compact']`, exactly as the Rollup build does. The
 * data is a running `poedex serve` when there is one. The geometry is arithmetic, not
 * taste: see {@link DEVICE} below.
 *
 * **Not real:** `@decky/ui`. Vite aliases it to `ui-kit/src/testing/decky-ui-double.tsx`
 * — the same stand-ins the test suite uses — because the package cannot be imported
 * outside Steam at all (it walks `window.webpackChunksteamui` at module scope). So
 * every Steam component here is a five-line approximation, the QAM inset is a CSS rule
 * reproducing SPEC §6.1's *measurement of a screenshot*, and the focus system is
 * `gamepad-focus.ts`'s model of Steam's rather than Steam's. The banner across the top
 * of this shell says so, and `docs/deck-checklist.md` marks per item what this can and
 * cannot settle.
 *
 * That distinction is the whole design of this tool. It exists because six Deck defects
 * were each found by build → copy → install → restart → `journalctl`, and most of them
 * were about layout and navigation. It does not exist to shorten the checklist to zero,
 * and a claim that it does would cost more than the tool saves.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import { Panel } from '@poedex/decky/Panel'
import { GamepadFocus } from '@poedex/ui/testing/gamepad'
import type { Direction, Rect } from '@poedex/ui/testing/gamepad'
import './preview.css'

/**
 * The Deck's screen, in the two units that matter.
 *
 * 1280 × 800 device pixels; gaming mode's UI runs at a **1.5× scale factor**, so the
 * CSS viewport a plugin sees is 853 × 533. The QAM column is **300 of those CSS px**
 * (SPEC §6.1), which is 450 device px — a bit over a third of the screen.
 *
 * The frame below is laid out in device pixels and the panel inside it is laid out at
 * 300 CSS px and then scaled by 1.5. That ordering is the point: every size the
 * `compact` profile writes — the 22 px grid cell, `fontSize: 9.5` — resolves against a
 * 300 px box exactly as it does on hardware, and the scale is applied afterwards as a
 * pure visual transform. A preview that instead drew a 450 px-wide panel would be
 * showing a layout the Deck never computes.
 */
const DEVICE = {
  width: 1280,
  height: 800,
  /** Gaming mode's UI scale. `853 × 533` CSS px is 1280 × 800 ÷ this. */
  scale: 1.5,
  /** SPEC §6.1. The QAM column, in CSS px. */
  qamWidth: 300,
} as const

const QAM_DEVICE_WIDTH = DEVICE.qamWidth * DEVICE.scale // 450
const QAM_CSS_HEIGHT = DEVICE.height / DEVICE.scale // 533.33

/** Breathing room around the device frame. Applied inline so the fit maths can subtract
 * the same number the layout uses; a stylesheet copy of it drifts silently. */
const STAGE_PAD = 12

export type DataMode = 'backend' | 'fixtures'

export interface PreviewProps {
  mode: DataMode
  /** Why the mode is what it is — shown, never guessed at by the reader. */
  note: string
}

type Button = Direction | 'a' | 'b' | 'x' | 'y'

interface Landing {
  /** What the press did, in the words the engine uses. */
  outcome: string
  /** The element focus ended on, described for a human. */
  target: string | null
  /** Its box, in the panel's own CSS pixels — the units SPEC §6.1 is written in. */
  size: string | null
}

export function Preview({ mode, note }: PreviewProps): ReactElement {
  const stage = useRef<HTMLDivElement>(null)
  const qam = useRef<HTMLDivElement>(null)
  const [zoom, setZoom] = useState(1)
  const [fit, setFit] = useState(true)
  const [landing, setLanding] = useState<Landing>({
    outcome: 'nothing pressed yet',
    target: null,
    size: null,
  })
  const [ring, setRing] = useState<Rect | null>(null)

  // ---- the outer zoom. A display transform only: nothing inside it re-lays out ----
  useLayoutEffect(() => {
    if (!fit) {
      setZoom(1)
      return
    }
    const measure = () => {
      const box = stage.current?.getBoundingClientRect()
      if (!box) return
      const room = (available: number, needed: number) =>
        (available - 2 * STAGE_PAD) / needed
      setZoom(Math.min(1, room(box.width, DEVICE.width), room(box.height, DEVICE.height)))
    }
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [fit])

  /** Where the focus box goes, and what the readout says. */
  const describeFocus = useCallback(() => {
    const root = qam.current
    const active = document.activeElement
    if (!root || !(active instanceof HTMLElement) || !root.contains(active)) {
      setRing(null)
      return null
    }
    const box = active.getBoundingClientRect()
    const frame = root.getBoundingClientRect()
    const factor = zoom * DEVICE.scale
    setRing({
      left: (box.left - frame.left) / factor,
      top: (box.top - frame.top) / factor,
      right: (box.right - frame.left) / factor,
      bottom: (box.bottom - frame.top) / factor,
    })
    return {
      target: describe(active),
      size: `${round(box.width / factor)} × ${round(box.height / factor)} CSS px`,
    }
  }, [zoom])

  // Mouse clicks move focus too, and the readout has to agree with the panel.
  useEffect(() => {
    const root = qam.current
    if (!root) return
    const sync = () => {
      const found = describeFocus()
      if (found) setLanding((current) => ({ ...current, ...found }))
    }
    root.addEventListener('focusin', sync)
    window.addEventListener('resize', sync)
    return () => {
      root.removeEventListener('focusin', sync)
      window.removeEventListener('resize', sync)
    }
  }, [describeFocus])

  const press = useCallback(
    (button: Button) => {
      // Built per press rather than held: it keeps no state of its own —
      // `document.activeElement` is the state — so there is nothing to stale.
      const engine = qam.current ? new GamepadFocus(qam.current) : null
      if (!engine) return
      let outcome: string
      if (button === 'a') {
        outcome = engine.activate() === 'activated' ? 'A — activated' : 'A — nothing focused'
      } else if (button === 'b') {
        const { handled } = engine.cancel()
        outcome = handled
          ? 'B — the panel went back (it stopped the event)'
          : 'B — nothing consumed it: the real QAM would close here'
      } else if (button === 'x' || button === 'y') {
        // Stated rather than silently swallowed. Nothing in this tree binds X or Y —
        // there is no `onButtonDown` anywhere — but three screens *draw* an X or Y
        // hint on an `Action`, which only **A** activates once focus is on it. The
        // preview names the control rather than shrugging, because "the glyph says Y
        // and Y does nothing" is exactly the sort of thing that reads as fine in a
        // screenshot and is wrong in the hand.
        const letter = button.toUpperCase()
        const hinted = hintedControl(qam.current, letter)
        outcome = hinted
          ? `${letter} — nothing binds ${letter}. "${hinted}" draws a ${letter} hint; only A activates it`
          : `${letter} — this panel binds no ${letter}, and draws no ${letter} hint`
      } else {
        outcome = engine.move(button) === 'moved' ? `${button} — moved` : `${button} — refused (edge)`
      }
      const found = describeFocus()
      setLanding({ outcome, target: found?.target ?? null, size: found?.size ?? null })
    },
    [describeFocus],
  )

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      // **Only real keystrokes.** `GamepadFocus` presses B by dispatching an `Escape`
      // keydown into the panel, and this listener sees that one too — on the way down,
      // since it captures. Pressing B in response to our own B is an infinite loop.
      // `isTrusted` is false for anything script-dispatched, which separates the two.
      if (!event.isTrusted) return
      const button = KEYS[event.key]
      if (!button) return
      event.preventDefault()
      // **And the panel must not see the real key at all.** React attaches its
      // listeners at the root container, which is *below* this one, so a bubbling
      // listener would arrive second — after the panel had already handled `Escape`
      // as B and `Enter` as A. Two bad things followed, both measured here before
      // this line existed: `A` fired `onActivate` twice for one press (once from
      // React's keydown, once from our click), and a **B** the panel consumed never
      // reached the readout, because React's `stopPropagation` also stops the native
      // event — so the one press that worked was the one the shell could not report.
      event.stopPropagation()
      press(button)
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [press])

  return (
    <div className="pv">
      <header className="pv__bar">
        <span className="pv__brand">Deck preview</span>
        <span className={`pv__chip pv__chip--${mode}`}>
          {mode === 'backend' ? 'live backend' : 'fixtures'}
        </span>
        <span className="pv__note">{note}</span>
        <button
          type="button"
          className="pv__toggle"
          onClick={() => setFit((current) => !current)}
          onMouseDown={(event) => event.preventDefault()}
        >
          {fit ? `fit · ${Math.round(zoom * 100)}%` : '1 : 1'}
        </button>
      </header>

      <div className="pv__stage" ref={stage} style={{ padding: STAGE_PAD }}>
        {/* Two boxes, and both are load-bearing. The outer one is the *layout* size —
            what the frame takes up on this page — and the inner one is the Deck's own
            1280 × 800, scaled down to meet it. Scaling a box that is still 1280 wide
            in the layout leaves the browser centring something bigger than its
            container, which it resolves by clamping to the top-left. */}
        <div style={{ width: DEVICE.width * zoom, height: DEVICE.height * zoom }}>
          <div
            className="pv__deck"
            style={{
              width: DEVICE.width,
              height: DEVICE.height,
              transform: `scale(${zoom})`,
            }}
          >
            <div className="pv__game">
              <p>
                1280 × 800 device px · gaming mode renders at {DEVICE.scale}×, so the CSS
                viewport is {Math.round(DEVICE.width / DEVICE.scale)} ×{' '}
                {Math.round(DEVICE.height / DEVICE.scale)}
              </p>
              <p className="pv__game-note">
                The game is not drawn. Only the panel is real here.
              </p>
            </div>

            <div className="pv__qam" style={{ width: QAM_DEVICE_WIDTH }}>
              <div
                className="pv__scale"
                style={{
                  width: DEVICE.qamWidth,
                  height: QAM_CSS_HEIGHT,
                  transform: `scale(${DEVICE.scale})`,
                }}
              >
                <div className="pv__panel" ref={qam}>
                  <Panel />
                </div>
              </div>
              {ring ? (
                <div
                  className="pv__ring"
                  style={{
                    left: ring.left * DEVICE.scale,
                    top: ring.top * DEVICE.scale,
                    width: (ring.right - ring.left) * DEVICE.scale,
                    height: (ring.bottom - ring.top) * DEVICE.scale,
                  }}
                />
              ) : null}
            </div>
          </div>
        </div>
      </div>

      <footer className="pv__hud">
        <div className="pv__pad">
          {(['left', 'up', 'down', 'right', 'a', 'b', 'x', 'y'] as const).map((button) => (
            <button
              key={button}
              type="button"
              onClick={() => press(button)}
              onMouseDown={(event) => event.preventDefault()}
            >
              {LABELS[button]}
            </button>
          ))}
        </div>

        <dl className="pv__readout">
          <dt>press</dt>
          <dd>{landing.outcome}</dd>
          <dt>focus</dt>
          <dd>{landing.target ?? '— nothing focused'}</dd>
          <dt>box</dt>
          <dd>{landing.size ?? '—'}</dd>
        </dl>

        <p className="pv__caveat">
          Arrow keys are the D-pad · <kbd>Enter</kbd> is A · <kbd>Esc</kbd> is B ·
          <kbd>x</kbd>/<kbd>y</kbd> are X and Y. Focus is
          resolved geometrically from live rects, the way Steam does it — but this is a
          <strong> model of </strong> Steam's focus system, and these are stand-ins for
          Steam's components, not Steam's components. It cannot tell you anything about
          real <code>@decky/ui</code>, suspend/resume, hot reload or the plugin host.
          See <code>docs/deck-checklist.md</code>.
        </p>
      </footer>
    </div>
  )
}

const KEYS: Record<string, Button | undefined> = {
  ArrowUp: 'up',
  ArrowDown: 'down',
  ArrowLeft: 'left',
  ArrowRight: 'right',
  Enter: 'a',
  ' ': 'a',
  Escape: 'b',
  Backspace: 'b',
  // The panel has no text input at any density, so plain letters are free. X and Y
  // are here because they are the two the panel *draws* and does not bind, and the
  // only way to find that out is to be able to press them.
  x: 'x',
  y: 'y',
  X: 'x',
  Y: 'y',
}

const LABELS: Record<Button, string> = {
  left: '←',
  up: '↑',
  down: '↓',
  right: '→',
  a: 'A',
  b: 'B',
  x: 'X',
  y: 'Y',
}

const round = (value: number) => Math.round(value * 10) / 10

/**
 * The control drawing an `X`/`Y` glyph, if there is one.
 *
 * `Action` renders its hint as a bare `<span>` with no attribute of its own, so this
 * matches on the glyph text and reads the label beside it. Fragile by nature — it is a
 * readout in a dev tool, not a contract — and it fails by saying nothing.
 */
function hintedControl(root: HTMLElement | null, letter: string): string | null {
  for (const button of Array.from(root?.querySelectorAll('button') ?? [])) {
    const [glyph, ...rest] = Array.from(button.childNodes)
    if (glyph?.textContent?.trim() !== letter) continue
    const label = rest
      .map((node) => node.textContent ?? '')
      .join('')
      .trim()
    if (label) return label
  }
  return null
}

/** Enough to tell two grid cells apart, and short enough for one line. */
function describe(element: HTMLElement): string {
  const kind = element.dataset.decky ?? element.tagName.toLowerCase()
  const label =
    element.getAttribute('aria-label') ??
    element.getAttribute('data-uid') ??
    (element.textContent ?? '').trim().replace(/\s+/g, ' ').slice(0, 64)
  return label ? `${kind} · ${label}` : kind
}
