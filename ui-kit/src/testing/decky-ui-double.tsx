/**
 * A stand-in for `@decky/ui`, used by both vitest configs.
 *
 * ## Why this file has to exist
 *
 * `@decky/ui` cannot be imported outside Steam. Its `webpack.js` runs
 * `initModuleCache()` at module scope, which pushes onto `window.webpackChunksteamui`
 * and then walks every chunk; with no chunk registry present, `components/Dialog.js`
 * evaluates `Object.values(CommonUIModule)` on `undefined` and the import throws
 * before a single test can run. Measured, not assumed — the traceback is in the Phase
 * 7 notes.
 *
 * So `resolve.alias` in `vitest.config.ts` and `vitest.compact.config.ts` points
 * `@decky/ui` here. `ui-kit/src/profiles/compact/steam.tsx` — the only file that
 * imports the package — therefore gets these components in a test run and the real
 * ones in a plugin build, where `@decky/rollup` externalises the whole package to the
 * global `DFL`.
 *
 * ## What this does and does not prove
 *
 * Every component tags its DOM node with `data-decky="<Name>"` and mirrors the prop
 * it was handed onto a `data-*` attribute. That is what lets `steam.test.tsx` assert
 * that the compact primitives really do delegate — that `ItemGrid` passes
 * `noFocusRing`, that a check row passes `onActivate`, that `Section` passes its title
 * to `PanelSection` — rather than merely claiming it. Running the 165-test suite
 * through this profile then exercises those delegating code paths.
 *
 * **It does not prove anything about real `@decky/ui`.** These are five-line
 * approximations of Steam components whose real behaviour is gamepad focus
 * navigation, and nobody on this project has a Steam Deck. Everything that needs the
 * hardware is enumerated in `docs/deck-checklist.md`; this file is not a substitute
 * for it and must not be described as one.
 *
 * The approximations, stated so they can be checked against the real thing:
 *
 * * `Focusable.onActivate` fires on click and on Enter/Space. On hardware it is the
 *   **A** button, and Steam decides which element has focus geometrically.
 * * `onGamepadFocus` / `onGamepadBlur` fire on DOM focus/blur. On hardware they fire
 *   when the D-pad moves onto and off the element.
 * * `noFocusRing`, `focusClassName` and `flow-children` are recorded and otherwise
 *   ignored. Their real effect is visual and navigational.
 * * `PanelSection` renders its title and children in a div. On hardware it also
 *   supplies the QAM inset that takes the column from 300 CSS px to 268.
 */

import type { CSSProperties, ReactNode } from 'react'

type Rest = Record<string, unknown>

/** Test-visible record of the props a double was handed, minus `children`. */
function marker(name: string, props: Rest): Rest {
  const out: Rest = { 'data-decky': name }
  for (const [key, value] of Object.entries(props)) {
    if (key.startsWith('data-') || key.startsWith('aria-') || key === 'role') out[key] = value
  }
  return out
}

export interface FocusableProps {
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
}

export const Focusable = ({
  children,
  style,
  className,
  focusClassName,
  noFocusRing,
  'flow-children': flowChildren,
  tabIndex,
  onActivate,
  onCancel: _onCancel,
  onGamepadFocus,
  onGamepadBlur,
  ...rest
}: FocusableProps) => (
  <div
    {...marker('Focusable', rest as Rest)}
    style={style}
    className={className}
    tabIndex={tabIndex ?? 0}
    data-no-focus-ring={noFocusRing ? 'true' : undefined}
    data-focus-class={focusClassName}
    data-flow-children={flowChildren}
    data-on-activate={onActivate ? 'true' : undefined}
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

export const PanelSection = ({ title, children }: { title?: string; children?: ReactNode }) => (
  <div data-decky="PanelSection" data-panel-title={title}>
    {title ? <div data-decky="PanelSectionTitle">{title}</div> : null}
    {children}
  </div>
)

export const PanelSectionRow = ({ children }: { children?: ReactNode }) => (
  <div data-decky="PanelSectionRow">{children}</div>
)

export const ScrollPanel = ({ children }: { children?: ReactNode }) => (
  <div data-decky="ScrollPanel" style={{ overflowY: 'auto' }}>
    {children}
  </div>
)

export const ScrollPanelGroup = ScrollPanel

export interface DialogButtonProps {
  children?: ReactNode
  style?: CSSProperties
  className?: string
  disabled?: boolean
  noFocusRing?: boolean
  onClick?: (event: unknown) => void
  onGamepadFocus?: (event: unknown) => void
  onGamepadBlur?: (event: unknown) => void
}

export const DialogButton = ({
  children,
  style,
  className,
  disabled,
  noFocusRing,
  onClick,
  onGamepadFocus,
  onGamepadBlur,
  ...rest
}: DialogButtonProps) => (
  <button
    type="button"
    {...marker('DialogButton', rest as Rest)}
    style={style}
    className={className}
    disabled={disabled}
    data-no-focus-ring={noFocusRing ? 'true' : undefined}
    onClick={onClick ? (event) => onClick(event) : undefined}
    onFocus={onGamepadFocus ? (event) => onGamepadFocus(event) : undefined}
    onBlur={onGamepadBlur ? (event) => onGamepadBlur(event) : undefined}
  >
    {children}
  </button>
)

export const DialogButtonPrimary = DialogButton
export const DialogButtonSecondary = DialogButton
export const Button = DialogButton

export interface ButtonItemProps {
  children?: ReactNode
  label?: ReactNode
  description?: ReactNode
  disabled?: boolean
  layout?: 'below' | 'inline'
  bottomSeparator?: 'standard' | 'thick' | 'none'
  onClick?: (event: unknown) => void
}

export const ButtonItem = ({
  children,
  label,
  description,
  disabled,
  layout,
  bottomSeparator,
  onClick,
  ...rest
}: ButtonItemProps) => (
  <button
    type="button"
    {...marker('ButtonItem', rest as Rest)}
    disabled={disabled}
    data-layout={layout}
    data-bottom-separator={bottomSeparator}
    onClick={onClick ? (event) => onClick(event) : undefined}
  >
    {label}
    {children}
    {description}
  </button>
)

export interface FieldProps {
  children?: ReactNode
  label?: ReactNode
  description?: ReactNode
  bottomSeparator?: 'standard' | 'thick' | 'none'
  padding?: 'none' | 'standard' | 'compact'
  childrenLayout?: 'below' | 'inline'
  childrenContainerWidth?: 'min' | 'max' | 'fixed'
  focusable?: boolean
  className?: string
}

export const Field = ({
  children,
  label,
  description,
  bottomSeparator,
  padding,
  childrenLayout,
  childrenContainerWidth,
  focusable,
  className,
  ...rest
}: FieldProps) => (
  <div
    {...marker('Field', rest as Rest)}
    className={className}
    data-padding={padding}
    data-bottom-separator={bottomSeparator}
    data-children-layout={childrenLayout}
    data-children-width={childrenContainerWidth}
    data-focusable={focusable ? 'true' : undefined}
    style={{ display: 'flex', alignItems: 'center', gap: 6 }}
  >
    {label ? <span style={{ flex: 1, minWidth: 0 }}>{label}</span> : null}
    {children}
    {description ? <span>{description}</span> : null}
  </div>
)

export const FocusRing = ({ children }: { children?: ReactNode }) => (
  <div data-decky="FocusRing">{children}</div>
)

/** `@decky/ui` exports this and a plugin entry calls it. Nothing here uses it. */
export const definePlugin = (fn: unknown) => fn
