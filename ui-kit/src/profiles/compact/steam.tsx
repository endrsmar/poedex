/**
 * The one file in this repository that imports `@decky/ui`, and the guard around it.
 *
 * ## Why a guard at all
 *
 * `@decky/ui` does not ship the components it exports. It *finds* them at load time
 * by walking Steam's webpack chunk registry and matching **regexes against minified
 * code** — `findModule(m => m.toString().includes('gamepaddialog_Field'))` and its
 * cousins. That works until Valve reships the client, at which point one of those
 * filters can miss and the export comes back `undefined`. A React tree that renders
 * `<undefined>` throws during render, and a throw inside the QAM is a **white panel**
 * with no message in it.
 *
 * That is not a hypothetical: SPEC §7 lists it as budgeted maintenance, and
 * `research-notes.md` §5 says the same. So every Steam component this profile uses is
 * resolved through {@link pick}, which substitutes a plain-DOM stand-in and records
 * the miss in {@link MISSING_STEAM_COMPONENTS}. The panel then renders *degraded and
 * legible* rather than blank, and the Decky shell puts the list of missing components
 * on screen so the failure names itself.
 *
 * ## What the stand-ins are and are not
 *
 * They are **structural**, five lines each: a `div` with a `tabIndex`, a `button`, a
 * titled `div`. They deliberately do **not** reproduce Steam's look, its focus
 * navigation or its gamepad handling — a fake that looked right would hide the
 * failure it exists to expose. Everything that makes the compact profile *the compact
 * profile* — the 300 px budget, the two-line check rows, the truncate-and-report rule,
 * the wording — lives in `index.tsx` and is identical on both paths.
 *
 * ## How this is built, and how it is tested
 *
 * `@decky/rollup` externalises `@decky/ui` to the global `DFL`, so in the shipped
 * bundle these imports compile to property reads off an object the loader already
 * evaluated. A missing component is therefore `undefined` — never an import error —
 * which is exactly the case {@link pick} handles.
 *
 * Under vitest the package cannot be imported at all: `@decky/ui` evaluates
 * `Object.values(CommonUIModule)` at module scope and throws `TypeError: Cannot
 * convert undefined or null to object` when Steam's chunk registry is absent. Both
 * vitest configs therefore alias `@decky/ui` to `ui-kit/testing/decky-ui-double.tsx`,
 * a set of minimal stand-ins that record the props they were handed. That is what
 * makes "the compact profile delegates to `@decky/ui`" an assertion rather than a
 * claim — see `steam.test.tsx`, which covers both the delegating and the degraded
 * path. **What it cannot check is real `@decky/ui` on real hardware**; that is item 2
 * of `docs/deck-checklist.md`.
 */

import type { ComponentType, CSSProperties, ReactNode } from 'react'
import {
  ButtonItem as SteamButtonItem,
  DialogButton as SteamDialogButton,
  Field as SteamField,
  Focusable as SteamFocusable,
  PanelSection as SteamPanelSection,
  PanelSectionRow as SteamPanelSectionRow,
  ScrollPanel as SteamScrollPanel,
} from '@decky/ui'

/**
 * Components `@decky/ui` did not hand us, in load order.
 *
 * Read by the Decky shell (`surfaces/decky`) and put on screen. An empty array is
 * the normal case and says nothing; a non-empty one is the single most useful thing
 * to know when the panel looks wrong after a Steam update.
 */
export const MISSING_STEAM_COMPONENTS: string[] = []

/**
 * The focus class the QAM stylesheet may hook, paired with `noFocusRing`.
 *
 * `noFocusRing` is not decoration: SPEC §6.1 measured Steam's default ring thick
 * enough to **merge adjacent 22 px grid cells**, which turns a bag map into a blur
 * exactly where the map is the point. The visible ring is then drawn inline off
 * `onGamepadFocus`/`onGamepadBlur` rather than from a stylesheet, because a plugin
 * cannot ship global CSS into Steam's document and this profile's rule is inline
 * styles only. The class name is still passed so a user stylesheet has a hook.
 */
export const FOCUS_CLASS = 'poedex-focus'

function pick<P>(name: string, found: ComponentType<P> | undefined, fallback: ComponentType<P>) {
  if (found) return found
  MISSING_STEAM_COMPONENTS.push(name)
  return fallback
}

// ------------------------------------------------------------- stand-ins ----

/** The props this profile actually passes. Narrower than `@decky/ui`'s, on purpose:
 * a stand-in that accepted everything would be a second implementation to maintain. */
export interface FocusableLike {
  children?: ReactNode
  style?: CSSProperties
  className?: string
  focusClassName?: string
  noFocusRing?: boolean
  'flow-children'?: string
  role?: string
  tabIndex?: number
  onActivate?: (event: unknown) => void
  onCancel?: (event: unknown) => void
  onGamepadFocus?: (event: unknown) => void
  onGamepadBlur?: (event: unknown) => void
  [key: `data-${string}`]: unknown
  [key: `aria-${string}`]: unknown
}

/**
 * A focusable div.
 *
 * `onActivate` is Steam's A button. Mapped to click and to Enter/Space so the same
 * tree is operable with a pointer — which is what the test suite drives it with, and
 * what a desktop CEF window gives you when you are debugging the panel over the
 * network. `onGamepadFocus` is mapped to DOM focus, which is the closest honest
 * approximation and is what makes the detail line under the grid work off a pointer
 * too.
 */
const FallbackFocusable = ({
  children,
  onActivate,
  onCancel: _onCancel,
  onGamepadFocus,
  onGamepadBlur,
  noFocusRing: _noFocusRing,
  focusClassName: _focusClassName,
  'flow-children': _flowChildren,
  tabIndex,
  ...rest
}: FocusableLike) => (
  <div
    {...rest}
    tabIndex={tabIndex ?? 0}
    onClick={onActivate ? (event) => onActivate(event) : undefined}
    onKeyDown={
      onActivate
        ? (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault()
              onActivate(event)
            }
          }
        : undefined
    }
    onFocus={onGamepadFocus ? (event) => onGamepadFocus(event) : undefined}
    onBlur={onGamepadBlur ? (event) => onGamepadBlur(event) : undefined}
  >
    {children}
  </div>
)

export interface PanelSectionLike {
  title?: string
  children?: ReactNode
}

const FallbackPanelSection = ({ title, children }: PanelSectionLike) => (
  <div data-panel-section={title ?? ''}>
    {title ? <div data-panel-section-title="">{title}</div> : null}
    {children}
  </div>
)

const FallbackPanelSectionRow = ({ children }: { children?: ReactNode }) => (
  <div data-panel-section-row="">{children}</div>
)

const FallbackScrollPanel = ({ children }: { children?: ReactNode }) => (
  <div style={{ overflowY: 'auto' }}>{children}</div>
)

export interface DialogButtonLike {
  children?: ReactNode
  style?: CSSProperties
  className?: string
  disabled?: boolean
  noFocusRing?: boolean
  onClick?: (event: unknown) => void
  onGamepadFocus?: (event: unknown) => void
  onGamepadBlur?: (event: unknown) => void
  [key: `data-${string}`]: unknown
  [key: `aria-${string}`]: unknown
}

const FallbackDialogButton = ({
  children,
  onClick,
  noFocusRing: _noFocusRing,
  onGamepadFocus,
  onGamepadBlur,
  ...rest
}: DialogButtonLike) => (
  <button
    type="button"
    {...rest}
    onClick={onClick ? (event) => onClick(event) : undefined}
    onFocus={onGamepadFocus ? (event) => onGamepadFocus(event) : undefined}
    onBlur={onGamepadBlur ? (event) => onGamepadBlur(event) : undefined}
  >
    {children}
  </button>
)

export interface ButtonItemLike {
  children?: ReactNode
  label?: ReactNode
  description?: ReactNode
  disabled?: boolean
  layout?: 'below' | 'inline'
  bottomSeparator?: 'standard' | 'thick' | 'none'
  onClick?: (event: unknown) => void
  [key: `data-${string}`]: unknown
  [key: `aria-${string}`]: unknown
}

const FallbackButtonItem = ({
  children,
  label,
  description,
  layout: _layout,
  bottomSeparator: _bottomSeparator,
  onClick,
  ...rest
}: ButtonItemLike) => (
  <button type="button" {...rest} onClick={onClick ? (event) => onClick(event) : undefined}>
    {label}
    {children}
    {description}
  </button>
)

export interface FieldLike {
  children?: ReactNode
  label?: ReactNode
  description?: ReactNode
  bottomSeparator?: 'standard' | 'thick' | 'none'
  padding?: 'none' | 'standard' | 'compact'
  childrenLayout?: 'below' | 'inline'
  childrenContainerWidth?: 'min' | 'max' | 'fixed'
  focusable?: boolean
  className?: string
  [key: `data-${string}`]: unknown
  [key: `aria-${string}`]: unknown
}

const FallbackField = ({
  children,
  label,
  description,
  bottomSeparator: _bottomSeparator,
  padding: _padding,
  childrenLayout: _childrenLayout,
  childrenContainerWidth: _childrenContainerWidth,
  focusable: _focusable,
  ...rest
}: FieldLike) => (
  <div {...rest} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
    {label ? <span style={{ flex: 1, minWidth: 0 }}>{label}</span> : null}
    {children}
    {description ? <span>{description}</span> : null}
  </div>
)

// --------------------------------------------------------- what we export ----
//
// Resolution happens once, at module load, so `MISSING_STEAM_COMPONENTS` is complete
// before the first render and the shell can show it without waiting for a screen to
// fail. The casts are the price of narrowing `@decky/ui`'s prop types to the ones
// this profile passes; each is checked against the real `.d.ts` above its stand-in.

export const Focusable = pick(
  'Focusable',
  SteamFocusable as unknown as ComponentType<FocusableLike> | undefined,
  FallbackFocusable,
)

export const PanelSection = pick(
  'PanelSection',
  SteamPanelSection as unknown as ComponentType<PanelSectionLike> | undefined,
  FallbackPanelSection,
)

export const PanelSectionRow = pick(
  'PanelSectionRow',
  SteamPanelSectionRow as unknown as ComponentType<{ children?: ReactNode }> | undefined,
  FallbackPanelSectionRow,
)

export const ScrollPanel = pick(
  'ScrollPanel',
  SteamScrollPanel as unknown as ComponentType<{ children?: ReactNode }> | undefined,
  FallbackScrollPanel,
)

export const DialogButton = pick(
  'DialogButton',
  SteamDialogButton as unknown as ComponentType<DialogButtonLike> | undefined,
  FallbackDialogButton,
)

export const ButtonItem = pick(
  'ButtonItem',
  SteamButtonItem as unknown as ComponentType<ButtonItemLike> | undefined,
  FallbackButtonItem,
)

export const Field = pick(
  'Field',
  SteamField as unknown as ComponentType<FieldLike> | undefined,
  FallbackField,
)

/** One sentence for the shell, or `null` when everything resolved. */
export function steamComponentWarning(): string | null {
  if (MISSING_STEAM_COMPONENTS.length === 0) return null
  return (
    `Steam did not supply ${MISSING_STEAM_COMPONENTS.join(', ')}. ` +
    'The panel is drawn with plain fallbacks — gamepad navigation may be degraded. ' +
    'This usually means a Steam client update moved something @decky/ui looks for.'
  )
}
