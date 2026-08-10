import { Children, isValidElement, useCallback, useState } from 'react'
import type { ReactNode } from 'react'
import type {
  FocusComponent,
  RowComponent,
  ScreenComponent,
  SectionComponent,
  StackComponent,
} from '../../contracts'
import { resolveHint } from '../../profile'
import { hiddenLabel } from '../../sync'

const P = 'full' as const

export const Screen: ScreenComponent = ({
  id,
  title,
  subtitle,
  banner,
  actions,
  aside,
  children,
}) => (
  <div className="pk-screen" data-screen={id}>
    <header className="pk-screen__head">
      <div className="pk-screen__titles">
        <h1 className="pk-screen__title">{title}</h1>
        {subtitle ? <span className="pk-screen__subtitle">{subtitle}</span> : null}
      </div>
      {actions ? <div className="pk-screen__actions">{actions}</div> : null}
    </header>
    {banner ? <div className="pk-screen__banner">{banner}</div> : null}
    <div className="pk-screen__body">
      <main className="pk-screen__main">{children}</main>
      {aside ? <aside className="pk-screen__aside">{aside}</aside> : null}
    </div>
  </div>
)

export const Section: SectionComponent = ({
  title,
  meta,
  description,
  tone = 'neutral',
  limit,
  limitNoun = 'rows',
  onShowAll,
  collapsible,
  defaultCollapsed,
  children,
}) => {
  const canCollapse = resolveHint(collapsible, P, false)
  const [collapsed, setCollapsed] = useState(() => resolveHint(defaultCollapsed, P, false))
  // `Children.toArray` already drops null/undefined/booleans; the empty-string
  // filter is what keeps a conditional that renders '' from counting as a row.
  const all = Children.toArray(children).filter((child) => child !== '')
  const cap = resolveHint(limit, P, null)
  const shown = cap === null || cap >= all.length ? all : all.slice(0, Math.max(0, cap))
  const hidden = all.length - shown.length

  return (
    <section className="pk-section" data-tone={tone} role="group" aria-label={title}>
      {title || meta ? (
        <div className="pk-section__head">
          <button
            type="button"
            className="pk-section__title"
            data-collapsible={canCollapse}
            onClick={canCollapse ? () => setCollapsed((value) => !value) : undefined}
            aria-expanded={canCollapse ? !collapsed : undefined}
            disabled={!canCollapse}
          >
            {canCollapse ? (
              <span className="pk-section__caret" aria-hidden="true">
                {collapsed ? '▶' : '▼'}
              </span>
            ) : null}
            <span className="pk-label">{title}</span>
          </button>
          {meta ? <span className="pk-label">{meta}</span> : null}
        </div>
      ) : null}
      {description && !collapsed ? (
        <p className="pk-section__description">{description}</p>
      ) : null}
      {collapsed ? null : shown}
      {!collapsed && hidden > 0 ? (
        <button
          type="button"
          className="pk-section__more"
          data-pressable={Boolean(onShowAll)}
          onClick={onShowAll}
          disabled={!onShowAll}
        >
          {hiddenLabel(hidden, limitNoun, Boolean(onShowAll))}
        </button>
      ) : null}
    </section>
  )
}

export const Stack: StackComponent = ({ gap = 'sm', children }) => (
  <div className={`pk-stack pk-gap-${resolveHint(gap, P, 'sm')}`}>{children}</div>
)

export const Row: RowComponent = ({
  gap = 'sm',
  align = 'center',
  justify = 'start',
  wrap,
  children,
}) => {
  const wrapping = resolveHint(wrap, P, false)
  return (
    <div
      className={[
        'pk-row',
        `pk-gap-${resolveHint(gap, P, 'sm')}`,
        `pk-align-${align}`,
        `pk-justify-${justify}`,
        wrapping ? 'pk-wrap' : '',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {children}
    </div>
  )
}

/**
 * At `full` a focus region is a named group that reports which descendant holds
 * focus. Under `compact` the same component wraps `@decky/ui`'s `FocusRing`, which
 * is the whole reason it exists as a primitive rather than as a `div`.
 */
export const Focus: FocusComponent = ({ id, label, onFocusChange, children }) => {
  const onFocus = useCallback(
    (event: React.FocusEvent<HTMLDivElement>) => {
      const target = event.target as HTMLElement
      onFocusChange?.(target.dataset.uid ?? null)
    },
    [onFocusChange],
  )
  const onBlur = useCallback(
    (event: React.FocusEvent<HTMLDivElement>) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
        onFocusChange?.(null)
      }
    },
    [onFocusChange],
  )
  return (
    <div className="pk-focus" data-focus-region={id} aria-label={label} onFocus={onFocus} onBlur={onBlur}>
      {children}
    </div>
  )
}

/** Exported for the profile's own components; not part of the public contract. */
export function isRenderable(node: ReactNode): boolean {
  return isValidElement(node) || typeof node === 'string' || typeof node === 'number'
}
