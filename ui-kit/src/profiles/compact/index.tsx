/**
 * The `compact` profile: 300 CSS px, gamepad, `@decky/ui`, inline styles only.
 *
 * **Phase 7 owns this file.** Phase 5's job was to make sure the contracts in
 * `../../contracts` can be honoured here at all, and the way to find that out is to
 * write the implementation rather than to promise one. So every primitive is
 * present with the contract's signature and renders something sane; what is
 * *deferred* is the `@decky/ui` binding and the hardware work:
 *
 * | primitive | Phase 7 replaces the element with |
 * |---|---|
 * | `Screen`      | `PanelSection` + the QAM footer hint row |
 * | `Section`     | `PanelSection` (`title` is its own prop) |
 * | `ItemGrid`    | a CSS grid of `Focusable` with `noFocusRing` + `focusClassName` |
 * | `ItemRow`     | `Focusable`, driving `Detail` through `onGamepadFocus` |
 * | `Action`      | `DialogButton` / `ButtonItem` |
 * | `Focus`       | `FocusRing` |
 * | `StaleBanner` | `PanelSectionRow` |
 *
 * Everything else — the density hints, the truncation rule, the wording, the
 * countdown — is real and is what makes this a check on the contracts.
 *
 * Rules this file follows, because they are the constraints `compact` actually has:
 *
 * * **Inline styles only.** No stylesheet import, no `className`. Steam's UI is one
 *   document and a plugin cannot ship global CSS into it.
 * * **300 px, hard.** Nothing may set a width that assumes more. Where `full` shows
 *   a column, this shows a line; where `full` shows a rail, this shows a strip.
 * * **Nothing hover-only.** There is no cursor. A title attribute is not a feature.
 */

import { Children, useState } from 'react'
import type { CSSProperties } from 'react'
import type {
  ActionComponent,
  DetailComponent,
  DetailField,
  EmptyComponent,
  ErrorStateComponent,
  FocusComponent,
  ItemGridComponent,
  ItemRowComponent,
  ItemRowField,
  KitImplementation,
  PendingComponent,
  RowComponent,
  ScreenComponent,
  SectionComponent,
  StaleBannerComponent,
  StackComponent,
  StatComponent,
  TallyComponent,
  ValueBarComponent,
  VerdictPillComponent,
} from '../../contracts'
import { COMPACT_PROFILE, resolveHint } from '../../profile'
import { useCountdown } from '../../countdown'
import { formatCountdown, formatPriceCell, formatQuantity, hasPrice } from '../../format'
import { REFRESH_LABEL, hiddenLabel, syncMessage } from '../../sync'
import { PROVENANCE_LABEL, PROVENANCE_SHORT, VERDICT_GLYPH, VERDICT_LABEL } from '../../verdict'

const P = 'compact' as const

export const PROFILE = COMPACT_PROFILE

/** SteamOS gaming mode has no light theme, so these are literals, not tokens. */
const C = {
  panel: '#171d25',
  panel2: '#212a35',
  panel3: '#2b3742',
  sunken: '#10161d',
  line: '#323d4a',
  ink: '#c7d5e0',
  ink2: '#8b9aa8',
  ink3: '#667582',
  accent: '#58a0d8',
  keep: '#4ea86f',
  check: '#c9a13f',
  trash: '#55636f',
  unpriceable: '#9b7bc4',
} as const

const VERDICT_COLOUR = {
  keep: C.keep,
  check: C.check,
  trash: C.trash,
  unpriceable: C.unpriceable,
  // Dimmer than trash: this block is not a decision, and colour is how the panel
  // says "you are not being asked anything here".
  not_loot: '#5b6570',
} as const

const RARITY_COLOUR: Record<string, string> = {
  normal: '#c8c8c8',
  magic: '#8f8fff',
  rare: '#ffff77',
  unique: '#d08130',
  relic: '#c0a882',
  currency: '#aa9e82',
  gem: '#1ba29b',
  divination: '#c1c1c1',
  quest: '#4ae63a',
  prophecy: '#b54bff',
  unknown: '#9aa6b2',
}

const label: CSSProperties = {
  fontFamily: 'monospace',
  fontSize: 8.5,
  letterSpacing: '0.12em',
  textTransform: 'uppercase',
  color: C.ink3,
}

const tnum: CSSProperties = { fontFamily: 'monospace', fontVariantNumeric: 'tabular-nums' }

const GAPS = { none: 0, xs: 2, sm: 4, md: 8, lg: 12 } as const

// ---------------------------------------------------------------- layout ----

export const Screen: ScreenComponent = ({
  id,
  title,
  subtitle,
  banner,
  actions,
  aside,
  children,
}) => (
  <div
    data-screen={id}
    style={{
      width: 300,
      display: 'flex',
      flexDirection: 'column',
      background: C.panel,
      color: C.ink,
      fontSize: 12,
    }}
  >
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        padding: '8px 10px',
        borderBottom: `1px solid ${C.line}`,
      }}
    >
      <span style={{ fontSize: 13, fontWeight: 620 }}>{title}</span>
      {/* One line, truncated. "Allflame (from the character)" is the difference
          between a number you can check and a number you have to trust — a
          silently wrong league was a real bug here, so this is not decoration. */}
      {subtitle ? (
        <span
          style={{
            fontSize: 9,
            color: C.ink2,
            marginLeft: 'auto',
            minWidth: 0,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {subtitle}
        </span>
      ) : null}
    </div>
    {banner}
    <div style={{ padding: 10, display: 'flex', flexDirection: 'column', gap: 9 }}>{children}</div>
    {/* At 300 px the `aside` is a strip below the body, not a rail beside it. Same
        JSX, different layout — the reason `Screen` takes it as a prop at all. */}
    {aside ? <div style={{ padding: '0 10px 10px' }}>{aside}</div> : null}
    {actions ? (
      <div
        style={{
          borderTop: `1px solid ${C.line}`,
          padding: '6px 10px',
          display: 'flex',
          gap: 10,
          fontSize: 9.5,
          color: C.ink2,
        }}
      >
        {actions}
      </div>
    ) : null}
  </div>
)

export const Section: SectionComponent = ({
  title,
  meta,
  description,
  limit,
  limitNoun = 'rows',
  onShowAll,
  collapsible,
  defaultCollapsed,
  children,
}) => {
  const canCollapse = resolveHint(collapsible, P, false)
  const [collapsed, setCollapsed] = useState(() => resolveHint(defaultCollapsed, P, false))
  const all = Children.toArray(children).filter(Boolean)
  const cap = resolveHint(limit, P, null)
  const shown = cap === null || cap >= all.length ? all : all.slice(0, Math.max(0, cap))
  const hidden = all.length - shown.length
  return (
    <div role="group" aria-label={title} style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      {title || meta ? (
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          <span
            style={{ ...label, cursor: canCollapse ? 'pointer' : undefined }}
            onClick={canCollapse ? () => setCollapsed((value) => !value) : undefined}
          >
            {canCollapse ? (collapsed ? '▶ ' : '▼ ') : ''}
            {title}
          </span>
          {meta ? <span style={label}>{meta}</span> : null}
        </div>
      ) : null}
      {/* `description` is dropped at 300 px unless there is nothing else: a
          sentence costs three lines here and the title has already said it. */}
      {description && !collapsed && all.length === 0 ? (
        <span style={{ fontSize: 10, color: C.ink2 }}>{description}</span>
      ) : null}
      {collapsed ? null : shown}
      {!collapsed && hidden > 0 ? (
        <span style={{ ...label, cursor: onShowAll ? 'pointer' : undefined }} onClick={onShowAll}>
          {hiddenLabel(hidden, limitNoun, Boolean(onShowAll))}
        </span>
      ) : null}
    </div>
  )
}

export const Stack: StackComponent = ({ gap = 'sm', children }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: GAPS[resolveHint(gap, P, 'sm')] }}>
    {children}
  </div>
)

export const Row: RowComponent = ({
  gap = 'sm',
  align = 'center',
  justify = 'start',
  wrap,
  children,
}) => (
  <div
    style={{
      display: 'flex',
      gap: GAPS[resolveHint(gap, P, 'sm')],
      alignItems: align === 'start' ? 'flex-start' : align === 'end' ? 'flex-end' : align,
      justifyContent:
        justify === 'between' ? 'space-between' : justify === 'end' ? 'flex-end' : 'flex-start',
      // Wrapping is the `compact` default: 300 px turns a four-item row into a
      // horizontal scrollbar, and a horizontal scrollbar has no D-pad affordance.
      flexWrap: resolveHint(wrap, P, true) ? 'wrap' : 'nowrap',
    }}
  >
    {children}
  </div>
)

export const Focus: FocusComponent = ({ id, label: name, onFocusChange, children }) => (
  // Phase 7: `<FocusRing>` from @decky/ui.
  <div
    data-focus-region={id}
    aria-label={name}
    onFocus={(event) => onFocusChange?.((event.target as HTMLElement).dataset.uid ?? null)}
  >
    {children}
  </div>
)

// ------------------------------------------------------------------ data ----

export const Stat: StatComponent = ({ label: name, value, unit, prefix, secondary, note, size }) => {
  const scale = resolveHint(size, P, 'md')
  const fontSize = scale === 'lg' ? 26 : scale === 'md' ? 18 : 13
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <span style={label}>{name}</span>
      <span style={{ ...tnum, fontSize, fontWeight: 700, lineHeight: 1, color: '#e8eff5' }}>
        {/* The floor marker is a superscript here rather than a character stealing
            8 px from a 26 px number — which is why `prefix` is a prop. */}
        {prefix ? <sup style={{ fontSize: '0.5em', color: C.ink2 }}>{prefix}</sup> : null}
        {value}
        {unit ? <span style={{ fontSize: '0.5em', color: C.ink2 }}> {unit}</span> : null}
      </span>
      {secondary ? <span style={{ ...tnum, fontSize: 10, color: C.ink2 }}>{secondary}</span> : null}
      {note ? <span style={{ fontSize: 9.5, color: C.ink3, lineHeight: 1.3 }}>{note}</span> : null}
    </div>
  )
}

export const Tally: TallyComponent = ({ entries, onSelect, selected }) => (
  <div role="group" aria-label="verdict tally" style={{ display: 'flex', gap: 4 }}>
    {entries.map((entry) => (
      <div
        key={entry.id}
        data-uid={entry.id}
        onClick={onSelect ? () => onSelect(entry.id) : undefined}
        style={{
          flex: 1,
          minWidth: 0,
          borderRadius: 3,
          padding: '4px 5px',
          background: selected === entry.id ? C.panel3 : C.sunken,
          borderLeft: `3px solid ${VERDICT_COLOUR[entry.verdict ?? 'trash']}`,
          opacity: entry.muted ?? entry.count === 0 ? 0.55 : 1,
        }}
      >
        <div style={{ ...tnum, fontSize: 14, fontWeight: 700, lineHeight: 1.1 }}>{entry.count}</div>
        <div style={{ ...label, fontSize: 7.5 }}>{entry.label}</div>
      </div>
    ))}
  </div>
)

export const VerdictPill: VerdictPillComponent = ({ verdict, label: name, count, size }) => (
  <span
    style={{
      fontFamily: 'monospace',
      fontSize: resolveHint(size, P, 'sm') === 'sm' ? 8 : 9.5,
      textTransform: 'uppercase',
      letterSpacing: '0.1em',
      borderRadius: 999,
      padding: '1px 6px',
      whiteSpace: 'nowrap',
      color: verdict === 'trash' ? C.ink : '#0e1a12',
      background: verdict === 'trash' ? C.panel3 : VERDICT_COLOUR[verdict],
    }}
  >
    {VERDICT_GLYPH[verdict]} {count === undefined ? '' : count} {name ?? VERDICT_LABEL[verdict]}
  </span>
)

export const ItemGrid: ItemGridComponent = ({
  cells,
  cols,
  rows,
  selected,
  onSelect,
  dimmed = false,
  emptyLabel = 'bag empty',
  label: name = 'bag layout',
}) => {
  const columns = resolveHint(cols, P, 12)
  const rowCount = resolveHint(rows, P, 5)
  if (cells.length === 0) {
    return <div style={{ ...label, padding: 12, textAlign: 'center' }}>{emptyLabel}</div>
  }
  // Empty slots are simply absent, so the D-pad skips them (SPEC §6.1). At 300 px
  // a 12-wide grid gives 22 px cells, which is the measured figure.
  return (
    <div
      role="grid"
      aria-label={name}
      aria-busy={dimmed}
      style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${columns}, 1fr)`,
        gridTemplateRows: `repeat(${rowCount}, 1fr)`,
        gap: 2,
        opacity: dimmed ? 0.45 : 1,
      }}
    >
      {cells.map((cell) => (
        // Phase 7: `<Focusable onGamepadFocus={() => onSelect?.(cell.uid)}>`.
        <div
          key={cell.uid}
          data-uid={cell.uid}
          role="gridcell"
          aria-label={`${cell.label}, ${VERDICT_LABEL[cell.verdict]}`}
          onClick={onSelect ? () => onSelect(cell.uid) : undefined}
          style={{
            gridColumn: `${cell.x + 1} / span ${cell.w ?? 1}`,
            gridRow: `${cell.y + 1} / span ${cell.h ?? 1}`,
            aspectRatio: '1',
            borderRadius: 2,
            background: '#182029',
            border: `1px solid ${VERDICT_COLOUR[cell.verdict]}`,
            boxShadow: `inset 3px 0 0 ${VERDICT_COLOUR[cell.verdict]}`,
            outline: selected === cell.uid ? `2px solid ${C.accent}` : undefined,
            outlineOffset: -1,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            opacity: cell.verdict === 'trash' ? 0.55 : 1,
            fontFamily: 'monospace',
            fontWeight: 700,
            fontSize: 10,
            color: RARITY_COLOUR[cell.rarity] ?? C.ink,
          }}
        >
          {cell.glyph.slice(0, 1)}
        </div>
      ))}
    </div>
  )
}

const COMPACT_ROW_FIELDS: ItemRowField[] = ['subtitle', 'quantity', 'price']

export const ItemRow: ItemRowComponent = ({ item, selected = false, onSelect, fields }) => {
  const shown = new Set(resolveHint(fields, P, COMPACT_ROW_FIELDS))
  const price = item.price
  const priceText = formatPriceCell(price, 1)
  const subLine = [
    shown.has('subtitle') ? item.subtitle : null,
    shown.has('quantity') && item.quantity && item.quantity > 1
      ? formatQuantity(item.quantity)
      : null,
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    // Phase 7: `<Focusable onGamepadFocus={...} onActivate={...}>`.
    <div
      data-uid={item.uid}
      onClick={onSelect ? () => onSelect(item.uid) : undefined}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        padding: '5px 6px',
        borderRadius: 4,
        background: selected ? C.panel3 : C.sunken,
        border: `1px solid ${selected ? C.accent : 'transparent'}`,
        opacity: item.verdict === 'trash' ? 0.75 : 1,
      }}
    >
      <span
        style={{
          width: 3,
          alignSelf: 'stretch',
          borderRadius: 999,
          background: VERDICT_COLOUR[item.verdict],
          flex: 'none',
        }}
      />
      <span style={{ flex: 1, minWidth: 0 }}>
        <span
          style={{
            display: 'block',
            fontSize: 11,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            color: RARITY_COLOUR[item.rarity] ?? C.ink,
          }}
        >
          {item.name}
        </span>
        {/* One sub-line carries both, because 300 px has room for one. The
            quantity is never *dropped* when the subtitle is not asked for — a
            five-digit stack is the difference between 3 chaos and 3,200. */}
        {subLine ? (
          <span
            style={{
              ...tnum,
              display: 'block',
              fontSize: 8.5,
              color: C.ink3,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {subLine}
          </span>
        ) : null}
      </span>
      {shown.has('marks') && item.marks?.length ? (
        // At 300 px this is the *only* thing that makes a `check` row actionable:
        // the row has no number, and without the gate's reasons it says nothing at
        // all. The signal names fit even where their sentences do not.
        <span style={{ display: 'flex', gap: 2, flexWrap: 'wrap', flex: 'none', maxWidth: 96 }}>
          {item.marks.map((mark) => (
            <span
              key={mark.id}
              style={{
                ...label,
                fontSize: 7,
                border: `1px solid ${C.line}`,
                borderRadius: 2,
                padding: '0 2px',
              }}
            >
              {mark.label}
            </span>
          ))}
        </span>
      ) : null}
      {shown.has('price') ? (
        <span
          style={{
            ...tnum,
            flex: 'none',
            fontSize: 11,
            fontWeight: 700,
            color: hasPrice(price) ? '#e8eff5' : C.ink3,
          }}
        >
          {priceText}
          {shown.has('provenance') && hasPrice(price) ? (
            <span style={{ fontSize: 7.5, color: C.ink3 }}> {PROVENANCE_SHORT[price.provenance]}</span>
          ) : null}
        </span>
      ) : null}
    </div>
  )
}

export const ValueBar: ValueBarComponent = ({ bars, max, limit, label: name }) => {
  const cap = resolveHint(limit, P, 4)
  const shown = cap === null ? bars : bars.slice(0, Math.max(0, cap))
  const ceiling = max ?? Math.max(1, ...bars.map((bar) => bar.value))
  return (
    <div aria-label={name} style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
      {shown.map((bar) => (
        <div key={bar.id} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ flex: 1, height: 4, background: '#1d2630', borderRadius: 999 }}>
            <span
              style={{
                display: 'block',
                height: '100%',
                borderRadius: 999,
                background: C.accent,
                width: `${Math.min(100, Math.max(2, (bar.value / ceiling) * 100))}%`,
              }}
            />
          </span>
          <span style={{ ...tnum, fontSize: 10, width: 48, textAlign: 'right', color: '#e8eff5' }}>
            {bar.display ?? bar.value}
          </span>
        </div>
      ))}
      {shown.length < bars.length ? (
        <span style={label}>{bars.length - shown.length} more hidden</span>
      ) : null}
    </div>
  )
}

// ----------------------------------------------------------- interaction ----

export const Action: ActionComponent = ({
  label: name,
  onPress,
  kind = 'default',
  disabled = false,
  countdown,
  busy = false,
  hint,
}) => {
  const remaining = useCountdown(countdown)
  const blocked = disabled || busy || remaining !== null
  return (
    // Phase 7: `<DialogButton>`. The gamepad glyph is the QAM's own affordance.
    <span
      role="button"
      aria-disabled={blocked}
      aria-busy={busy}
      onClick={blocked ? undefined : onPress}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        fontSize: 10,
        color: kind === 'primary' ? '#f2f8fc' : C.ink,
        opacity: blocked ? 0.5 : 1,
      }}
    >
      {hint ? (
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: 13,
            height: 13,
            borderRadius: '50%',
            border: `1px solid ${C.ink3}`,
            fontFamily: 'monospace',
            fontSize: 8,
            fontWeight: 700,
          }}
        >
          {hint}
        </span>
      ) : null}
      {name}
      {remaining !== null ? <span style={tnum}> {formatCountdown(remaining)}</span> : null}
    </span>
  )
}

export const Detail: DetailComponent = ({ item, fields, empty = 'nothing selected', actions }) => {
  const shown = resolveHint<DetailField[]>(fields, P, ['name', 'value'])
  if (!item) return <div style={{ ...label, padding: 6 }}>{empty}</div>
  const price = item.price
  return (
    <div
      role="group"
      aria-label="item detail"
      style={{
        background: C.panel2,
        border: `1px solid ${C.line}`,
        borderRadius: 4,
        padding: 8,
        display: 'flex',
        flexDirection: 'column',
        gap: 4,
      }}
    >
      {shown.includes('name') ? (
        <span style={{ fontSize: 12, fontWeight: 620, color: RARITY_COLOUR[item.rarity] ?? C.ink }}>
          {item.name}
        </span>
      ) : null}
      {shown.includes('value') ? (
        <span style={{ ...tnum, fontSize: 14, fontWeight: 700 }}>
          {formatPriceCell(price, 1)}
        </span>
      ) : null}
      {shown.includes('provenance') && hasPrice(price) ? (
        <span style={{ ...label, fontSize: 8 }}>{PROVENANCE_LABEL[price.provenance]}</span>
      ) : null}
      {shown.includes('stack') && item.quantity && item.quantity > 1 ? (
        <span style={{ ...tnum, fontSize: 10, color: C.ink2 }}>{formatQuantity(item.quantity)}</span>
      ) : null}
      {shown.includes('reason') && item.reason ? (
        <span style={{ fontSize: 9.5, color: C.ink2 }}>{item.reason}</span>
      ) : null}
      {/* A verdict nobody can argue with is one nobody trusts. The signal names fit
          at 300 px even when their detail sentences do not. */}
      {shown.includes('gate') && item.gate?.considered ? (
        <span style={{ display: 'flex', gap: 3, flexWrap: 'wrap' }}>
          {item.gate.signals.map((signal) => (
            <span
              key={signal.id}
              style={{
                ...label,
                fontSize: 7.5,
                border: `1px solid ${C.line}`,
                borderRadius: 3,
                padding: '0 3px',
              }}
            >
              {signal.label}
            </span>
          ))}
        </span>
      ) : null}
      {/* Two bars. "Your price against the index" is SPEC §6.3's comparison and it
          costs eight pixels of height, so 300 px is not a reason to drop it. */}
      {shown.includes('comparables') && item.comparables?.length ? (
        <ValueBar bars={item.comparables} limit={2} label="your price vs market" />
      ) : null}
      {actions}
    </div>
  )
}

// ----------------------------------------------------------------- state ----

export const Pending: PendingComponent = ({ label: name = 'loading', rows }) => (
  <div aria-label={name} style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
    {Array.from({ length: resolveHint(rows, P, 3) }, (_unused, index) => (
      <div key={index} style={{ height: 22, borderRadius: 3, background: C.panel2 }} />
    ))}
  </div>
)

export const Empty: EmptyComponent = ({ title, detail, action }) => (
  <div style={{ padding: 8, display: 'flex', flexDirection: 'column', gap: 3 }}>
    <span style={{ fontSize: 11, fontWeight: 620 }}>{title}</span>
    {detail ? <span style={{ fontSize: 9.5, color: C.ink2 }}>{detail}</span> : null}
    {action}
  </div>
)

export const ErrorState: ErrorStateComponent = ({ title, detail, kind = 'failure', action }) => (
  <div
    role="alert"
    style={{
      padding: 8,
      borderLeft: `3px solid ${kind === 'failure' ? C.check : C.trash}`,
      background: C.sunken,
      display: 'flex',
      flexDirection: 'column',
      gap: 3,
    }}
  >
    <span style={{ fontSize: 11, fontWeight: 620 }}>{title}</span>
    {detail ? <span style={{ fontSize: 9.5, color: C.ink2 }}>{detail}</span> : null}
    {action}
  </div>
)

export const StaleBanner: StaleBannerComponent = ({
  status,
  onRefresh,
  // The same default word as `full`. Two profiles that name the same control
  // differently are two products; the gamepad glyph is the affordance difference.
  refreshLabel = REFRESH_LABEL,
  labels,
}) => {
  const remaining = useCountdown(status.retryAfter)
  const message = syncMessage(status, remaining, labels)
  return (
    <div
      role="status"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        padding: '5px 10px',
        borderBottom: `1px solid ${C.line}`,
        fontSize: 9.5,
        color: C.ink2,
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: '50%',
          flex: 'none',
          background:
            status.state === 'error'
              ? C.unpriceable
              : status.state === 'fresh'
                ? C.keep
                : status.state === 'syncing'
                  ? C.accent
                  : C.check,
        }}
      />
      <span
        style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
      >
        {message}
        {/* The reason is the most useful thing on a failed sync and is the first
            thing a 300 px layout is tempted to drop. It is truncated, not lost. */}
        {status.detail ? <span style={{ color: C.ink3 }}> — {status.detail}</span> : null}
      </span>
      {onRefresh ? (
        <span style={{ marginLeft: 'auto' }}>
          <Action
            label={refreshLabel}
            hint="Y"
            onPress={onRefresh}
            busy={status.state === 'syncing'}
            countdown={status.state === 'restricted' ? status.retryAfter : null}
          />
        </span>
      ) : null}
    </div>
  )
}

const _implementation: KitImplementation = {
  Screen,
  Section,
  Stack,
  Row,
  Focus,
  Stat,
  Tally,
  ItemGrid,
  ItemRow,
  ValueBar,
  VerdictPill,
  Action,
  Detail,
  Pending,
  Empty,
  ErrorState,
  StaleBanner,
}
void _implementation
