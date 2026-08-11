/**
 * The `compact` profile: 300 CSS px, gamepad, `@decky/ui`, inline styles only.
 *
 * **Phase 7 swapped the elements.** Phase 5 built this profile as a *working*
 * implementation in plain DOM so that `vitest.compact.config.ts` could run the whole
 * component suite through it — a harness that found five content drops and one forked
 * vocabulary on its first run, none of which a stubbed signature would have caught.
 * What Phase 7 changed is the element each primitive renders; what it did not change
 * is a single contract, a single hint, or a word of the copy.
 *
 * | primitive | now renders |
 * |---|---|
 * | `Screen`      | 300 px root, header, `ScrollPanel` body, `PanelSectionRow` hint row |
 * | `Section`     | `PanelSection` (`title` is its own prop); `ButtonItem` for "show all" |
 * | `ItemGrid`    | a CSS grid of `Focusable`, `noFocusRing` + `focusClassName` |
 * | `ItemRow`     | `Focusable`, driving `Detail` through `onGamepadFocus` |
 * | `Tally`       | `Focusable` per entry when it is a filter, a div when a readout |
 * | `Action`      | `DialogButton` |
 * | `CheckList`   | `Focusable onActivate` per row — A ticks the box |
 * | `Stepper`     | `Field` with two `DialogButton`s either side of the value |
 * | `Focus`       | `Focusable` with `flow-children` |
 * | `StaleBanner` | `PanelSectionRow` |
 *
 * Every one of those comes from `./steam`, which resolves each component through a
 * guard: `@decky/ui` locates Steam's components by regex over minified code, so a
 * client update can make one come back `undefined`, and rendering `<undefined>` is a
 * **white panel**. The guard substitutes a plain-DOM stand-in and names the miss.
 * See `steam.tsx`; the degradation is the point of it.
 *
 * Rules this file follows, because they are the constraints `compact` actually has:
 *
 * * **Inline styles only.** No stylesheet import, no `className` of our own. Steam's
 *   UI is one document and a plugin cannot ship global CSS into it.
 * * **300 px, hard.** Nothing may set a width that assumes more. Where `full` shows
 *   a column, this shows a line; where `full` shows a rail, this shows a strip.
 * * **Nothing hover-only.** There is no cursor. A title attribute is not a feature.
 * * **Focus is visible from an inline style, not from a class.** `noFocusRing` is
 *   passed because Steam's default ring merges adjacent 22 px cells (SPEC §6.1), and
 *   the replacement is drawn from `onGamepadFocus` state so it exists whether or not
 *   any stylesheet reached the document.
 */

import { Children, useCallback, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import type {
  ActionComponent,
  CheckField,
  CheckListComponent,
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
  StepperComponent,
  TallyComponent,
  ValueBarComponent,
  VerdictPillComponent,
} from '../../contracts'
import { COMPACT_PROFILE, resolveHint } from '../../profile'
import { useCountdown } from '../../countdown'
import { formatCountdown, formatPriceCell, formatQuantity, hasPrice } from '../../format'
import { REFRESH_LABEL, hiddenLabel, syncMessage } from '../../sync'
import { PROVENANCE_LABEL, PROVENANCE_SHORT, VERDICT_GLYPH, VERDICT_LABEL } from '../../verdict'
import {
  ButtonItem,
  DialogButton,
  Field,
  FOCUS_CLASS,
  Focusable,
  PanelSection,
  PanelSectionRow,
  ScrollPanel,
} from './steam'

const P = 'compact' as const

export const PROFILE = COMPACT_PROFILE

export { MISSING_STEAM_COMPONENTS, steamComponentWarning } from './steam'

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

/**
 * Half the width a `PanelSection` costs, and the derivation is the whole point.
 *
 * SPEC §6.1 measured the QAM column at **300 CSS px** and **268 inside a
 * `PanelSection`**. The difference is 32 px of inset, 16 a side, so a negative margin
 * of exactly that much on a grid restores the full column — which is the same
 * measurement's other half: cells go from 22 px to 24 px. It is applied to `ItemGrid`
 * and to nothing else, because the grid is the one thing on this surface that is a
 * *map* (SPEC §6.3) and a 2 px cell gain is a 9% target gain on every slot.
 *
 * **Unverified on hardware.** If `PanelSection`'s inset is not 16 px a side the grid
 * will sit proud of its section or short of it; `docs/deck-checklist.md` item 3 is
 * how that gets found, and the fix is this one number.
 */
const PANEL_INSET = 16

/** Steam's focus ring is suppressed; this is what replaces it. */
const focusOutline = (focused: boolean, colour = C.accent): CSSProperties =>
  focused ? { outline: `2px solid ${colour}`, outlineOffset: -1 } : {}

/**
 * Focus state for one cell or row.
 *
 * `onGamepadFocus` is how detail fits in 268 px (SPEC §6.1): the D-pad moving onto a
 * cell is what fills the line underneath the grid, so no press is spent on selection
 * and the player reads the bag by sweeping it. The same handlers drive the inline
 * focus ring, so the two never disagree about what is focused.
 */
function useGamepadFocus(onFocused?: () => void) {
  const [focused, setFocused] = useState(false)
  const onGamepadFocus = useCallback(() => {
    setFocused(true)
    onFocused?.()
  }, [onFocused])
  const onGamepadBlur = useCallback(() => setFocused(false), [])
  return { focused, onGamepadFocus, onGamepadBlur }
}

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
    {/* The banner is deliberately outside the ScrollPanel: a sync state that scrolls
        away is a sync state that is not doing its job. */}
    {banner}
    <ScrollPanel>
      <div style={{ padding: 10, display: 'flex', flexDirection: 'column', gap: 9 }}>{children}</div>
      {/* At 300 px the `aside` is a strip below the body, not a rail beside it. Same
          JSX, different layout — the reason `Screen` takes it as a prop at all. */}
      {aside ? <div style={{ padding: '0 10px 10px' }}>{aside}</div> : null}
    </ScrollPanel>
    {actions ? (
      <PanelSectionRow>
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
      </PanelSectionRow>
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
  const footer = hidden > 0 ? hiddenLabel(hidden, limitNoun, Boolean(onShowAll)) : null
  return (
    // `PanelSection` owns the title chrome — that is what `title` is for — but this
    // profile draws its own, because the kit's title line carries `meta` on the right
    // (a count, a subtotal) and Steam's does not have a slot for it. The prop is still
    // passed so the section is labelled for anything reading the tree.
    <PanelSection title={title}>
      <div
        role="group"
        aria-label={title}
        style={{ display: 'flex', flexDirection: 'column', gap: 5 }}
      >
        {title || meta ? (
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
            {canCollapse ? (
              <DialogButton
                noFocusRing
                aria-expanded={!collapsed}
                onClick={() => setCollapsed((value) => !value)}
                style={{
                  ...label,
                  background: 'none',
                  border: 'none',
                  padding: 0,
                  textAlign: 'left',
                }}
              >
                {collapsed ? '▶ ' : '▼ '}
                {title}
              </DialogButton>
            ) : (
              <span style={label}>{title}</span>
            )}
            {meta ? <span style={label}>{meta}</span> : null}
          </div>
        ) : null}
        {/* `description` is dropped at 300 px unless there is nothing else: a
            sentence costs three lines here and the title has already said it. */}
        {description && !collapsed && all.length === 0 ? (
          <span style={{ fontSize: 10, color: C.ink2 }}>{description}</span>
        ) : null}
        {collapsed ? null : shown}
        {!collapsed && footer ? (
          onShowAll ? (
            // A `ButtonItem` rather than a `DialogButton`: this is a full-width row
            // press at the end of a list, which is the shape `ButtonItem` is for, and
            // at 300 px a row-sized target is the difference between one D-pad stop
            // and three.
            <ButtonItem layout="below" bottomSeparator="none" onClick={onShowAll}>
              <span style={label}>{footer}</span>
            </ButtonItem>
          ) : (
            <span style={label}>{footer}</span>
          )
        ) : null}
      </div>
    </PanelSection>
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
  // `flow-children="column"` tells Steam's navigation that this region reads
  // top-to-bottom; the geometric resolver still does the work inside it.
  <Focusable
    flow-children="column"
    data-focus-region={id}
    aria-label={name}
    onGamepadFocus={(event) =>
      onFocusChange?.(
        ((event as { target?: HTMLElement } | undefined)?.target?.dataset?.uid ?? null) as
          | string
          | null,
      )
    }
    onGamepadBlur={() => onFocusChange?.(null)}
  >
    {children}
  </Focusable>
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

function TallyEntryCell({
  entry,
  selected,
  onSelect,
}: {
  entry: { id: string; label: string; count: number; verdict?: string; muted?: boolean }
  selected?: string | null
  onSelect?: (id: string) => void
}) {
  const { focused, onGamepadFocus, onGamepadBlur } = useGamepadFocus()
  const style: CSSProperties = {
    flex: 1,
    minWidth: 0,
    borderRadius: 3,
    padding: '4px 5px',
    background: selected === entry.id ? C.panel3 : C.sunken,
    borderLeft: `3px solid ${VERDICT_COLOUR[(entry.verdict ?? 'trash') as keyof typeof VERDICT_COLOUR]}`,
    opacity: entry.muted ?? entry.count === 0 ? 0.55 : 1,
    ...focusOutline(focused),
  }
  const body = (
    <>
      <div style={{ ...tnum, fontSize: 14, fontWeight: 700, lineHeight: 1.1 }}>{entry.count}</div>
      <div style={{ ...label, fontSize: 7.5 }}>{entry.label}</div>
    </>
  )
  // A readout is not a focus stop. Making every tally a `Focusable` would put four
  // extra D-pad stops between the total and the grid on a screen that is mostly grid.
  if (!onSelect) {
    return (
      <div data-uid={entry.id} style={style}>
        {body}
      </div>
    )
  }
  return (
    <Focusable
      noFocusRing
      focusClassName={FOCUS_CLASS}
      data-uid={entry.id}
      style={style}
      onActivate={() => onSelect(entry.id)}
      onGamepadFocus={onGamepadFocus}
      onGamepadBlur={onGamepadBlur}
    >
      {body}
    </Focusable>
  )
}

export const Tally: TallyComponent = ({ entries, onSelect, selected }) => (
  <div role="group" aria-label="verdict tally" style={{ display: 'flex', gap: 4 }}>
    {entries.map((entry) => (
      <TallyEntryCell key={entry.id} entry={entry} selected={selected} onSelect={onSelect} />
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

function GridCell({
  cell,
  selected,
  onSelect,
}: {
  cell: {
    uid: string
    glyph: string
    label: string
    verdict: keyof typeof VERDICT_COLOUR
    rarity: string
    x: number
    y: number
    w?: number
    h?: number
  }
  selected?: string | null
  onSelect?: (uid: string) => void
}) {
  const { focused, onGamepadFocus, onGamepadBlur } = useGamepadFocus(
    onSelect ? () => onSelect(cell.uid) : undefined,
  )
  return (
    <Focusable
      // The default ring is thick enough to merge adjacent 22 px cells, which is
      // exactly the thing this grid exists to distinguish (SPEC §6.1).
      noFocusRing
      focusClassName={FOCUS_CLASS}
      data-uid={cell.uid}
      role="gridcell"
      aria-label={`${cell.label}, ${VERDICT_LABEL[cell.verdict]}`}
      // **Focus is selection here, and there is no `onActivate`.** SPEC §6.1's whole
      // reason for `onGamepadFocus` is that detail has to fit in 268 px: the D-pad
      // moving onto a cell fills the line underneath it, so the player reads the bag
      // by sweeping rather than by pressing. Wiring A to the same thing would fire
      // `onSelect` twice for one interaction — focus, then activate — and a module
      // that spends a request per selection (the stash tab list does) would spend two.
      onGamepadFocus={onGamepadFocus}
      onGamepadBlur={onGamepadBlur}
      style={{
        gridColumn: `${cell.x + 1} / span ${cell.w ?? 1}`,
        gridRow: `${cell.y + 1} / span ${cell.h ?? 1}`,
        aspectRatio: '1',
        borderRadius: 2,
        background: '#182029',
        border: `1px solid ${VERDICT_COLOUR[cell.verdict]}`,
        boxShadow: `inset 3px 0 0 ${VERDICT_COLOUR[cell.verdict]}`,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        opacity: cell.verdict === 'trash' ? 0.55 : 1,
        fontFamily: 'monospace',
        fontWeight: 700,
        fontSize: 10,
        color: RARITY_COLOUR[cell.rarity] ?? C.ink,
        ...focusOutline(focused || selected === cell.uid),
      }}
    >
      {cell.glyph.slice(0, 1)}
    </Focusable>
  )
}

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
  // Empty slots are simply absent, so the D-pad skips them (SPEC §6.1), and Steam's
  // focus system resolves direction geometrically against the live rects — so a plain
  // CSS grid of `Focusable` navigates in 2D with no handler here at all.
  return (
    <div
      role="grid"
      aria-label={name}
      aria-busy={dimmed}
      data-cols={columns}
      data-rows={rowCount}
      style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${columns}, 1fr)`,
        gridTemplateRows: `repeat(${rowCount}, 1fr)`,
        gap: 2,
        opacity: dimmed ? 0.45 : 1,
        // Back out of the `PanelSection` inset: 300 px of column instead of 268, and
        // 24 px cells instead of 22. See PANEL_INSET.
        marginLeft: -PANEL_INSET,
        marginRight: -PANEL_INSET,
      }}
    >
      {cells.map((cell) => (
        <GridCell key={cell.uid} cell={cell} selected={selected} onSelect={onSelect} />
      ))}
    </div>
  )
}

const COMPACT_ROW_FIELDS: ItemRowField[] = ['subtitle', 'quantity', 'price']

export const ItemRow: ItemRowComponent = ({ item, selected = false, onSelect, fields }) => {
  const { focused, onGamepadFocus, onGamepadBlur } = useGamepadFocus(
    onSelect ? () => onSelect(item.uid) : undefined,
  )
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
    <Focusable
      noFocusRing
      focusClassName={FOCUS_CLASS}
      data-uid={item.uid}
      // Focus is selection, and there is no `onActivate` — see `GridCell`.
      onGamepadFocus={onGamepadFocus}
      onGamepadBlur={onGamepadBlur}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        padding: '5px 6px',
        borderRadius: 4,
        background: selected ? C.panel3 : C.sunken,
        border: `1px solid ${selected ? C.accent : 'transparent'}`,
        opacity: item.verdict === 'trash' ? 0.75 : 1,
        ...focusOutline(focused),
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
    </Focusable>
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
    <DialogButton
      disabled={blocked}
      aria-busy={busy}
      onClick={blocked ? undefined : onPress}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        width: 'auto',
        minWidth: 0,
        padding: '2px 6px',
        fontSize: 10,
        color: kind === 'primary' ? '#f2f8fc' : C.ink,
        background: kind === 'quiet' ? 'transparent' : undefined,
        opacity: blocked ? 0.5 : 1,
      }}
    >
      {/* The gamepad glyph is the QAM's own affordance: the footer legend says what
          A and Y do, and a control that names its button is one the player can find
          without moving focus onto it first. */}
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
    </DialogButton>
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
    <PanelSectionRow>
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
    </PanelSectionRow>
  )
}

/**
 * The checkbox list at 300 px — **the design problem of Phase 9.**
 *
 * Six mods, each with a tier label, inside 268 usable pixels, navigated with a
 * D-pad. It does not fit as a row of columns, and the resolution is that it stops
 * being a row: a `compact` option is **two lines**, the mod text on the first and
 * the tier under it, and the whole thing is a 30 px focus target rather than a 14 px
 * checkbox somebody has to land on.
 *
 * Phase 7's swap is exactly one element: the row is now `<Focusable onActivate>`, so
 * **A ticks the box** and the 30 px target is what the D-pad lands on. The two-line
 * layout was already shaped for it, which is what made this a swap rather than a
 * redesign.
 *
 * Three things this deliberately does not do:
 *
 * * **It does not truncate.** Every other list in this kit takes a `limit` and
 *   reports what it hid; a checkbox list may not, because a hidden row is a filter
 *   the player cannot see and cannot switch off. Six affixes is the game's ceiling,
 *   so the list is bounded by the item.
 * * **It does not shorten the mod text.** `+120 to maximum Life` wraps to two lines
 *   at this width and that is correct: the player is matching it against the tooltip
 *   in game, and `+120 to maximu…` is not something they can match.
 * * **It does not hide a disabled row.** A mod with no trade filter is still on the
 *   item; it is drawn dimmed with an em-dash box, so the list still agrees with the
 *   item.
 */
function CheckRow({
  option,
  on,
  shown,
  onToggle,
}: {
  option: {
    id: string
    label: string
    badge?: string
    meta?: string
    tone?: string
    disabled?: boolean
    disabledReason?: string
  }
  on: boolean
  shown: Set<CheckField>
  onToggle: (id: string) => void
}) {
  const { focused, onGamepadFocus, onGamepadBlur } = useGamepadFocus()
  const body: ReactNode = (
    <>
      <span
        aria-hidden="true"
        style={{
          flex: 'none',
          width: 13,
          height: 13,
          marginTop: 1,
          borderRadius: 3,
          border: `1px solid ${on ? C.accent : C.line}`,
          background: on ? C.accent : 'transparent',
          color: C.panel,
          fontSize: 9,
          lineHeight: '12px',
          textAlign: 'center',
        }}
      >
        {option.disabled ? '–' : on ? '✓' : ''}
      </span>
      <span style={{ flex: 1, minWidth: 0 }}>
        {/* Wraps. A mod line the player cannot read off against the game's
            own tooltip is a mod line they cannot tick with any confidence. */}
        <span style={{ display: 'block', fontSize: 10.5, color: C.ink }}>{option.label}</span>
        {/* The tier goes *under* the text, not beside it: at 268 px a
            right-hand column costs the mod line its last eight characters,
            and the mod line is the thing being identified. */}
        {shown.has('badge') && option.badge ? (
          <span
            style={{
              ...tnum,
              display: 'inline-block',
              fontSize: 8.5,
              color: BADGE_COLOUR[option.tone ?? 'neutral'] ?? C.ink2,
            }}
          >
            {option.badge}
          </span>
        ) : null}
        {shown.has('meta') && option.meta ? (
          <span style={{ fontSize: 8.5, color: C.ink3 }}>
            {shown.has('badge') && option.badge ? ' · ' : ''}
            {option.meta}
          </span>
        ) : null}
      </span>
    </>
  )
  return (
    <Focusable
      noFocusRing
      focusClassName={FOCUS_CLASS}
      data-check-id={option.id}
      role="checkbox"
      aria-checked={on}
      aria-disabled={option.disabled ? true : undefined}
      aria-label={
        option.disabled && option.disabledReason
          ? `${option.label} — ${option.disabledReason}`
          : option.label
      }
      onActivate={option.disabled ? undefined : () => onToggle(option.id)}
      onGamepadFocus={onGamepadFocus}
      onGamepadBlur={onGamepadBlur}
      style={{
        display: 'flex',
        gap: 6,
        alignItems: 'flex-start',
        padding: '5px 6px',
        minHeight: 30,
        borderRadius: 4,
        background: on ? C.panel3 : C.sunken,
        border: `1px solid ${on ? C.accent : 'transparent'}`,
        opacity: option.disabled ? 0.5 : 1,
        ...focusOutline(focused),
      }}
    >
      {body}
    </Focusable>
  )
}

export const CheckList: CheckListComponent = ({
  options,
  selected,
  onToggle,
  label: name = 'mods to search on',
  fields,
  emptyLabel = 'no readable mods',
}) => {
  const shown = new Set(resolveHint<CheckField[]>(fields, P, ['badge']))
  const ticked = new Set(selected)
  if (options.length === 0) {
    return <div style={{ ...label, padding: 4 }}>{emptyLabel}</div>
  }
  return (
    <div role="group" aria-label={name} style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
      {options.map((option) => (
        <CheckRow
          key={option.id}
          option={option}
          on={ticked.has(option.id)}
          shown={shown}
          onToggle={onToggle}
        />
      ))}
    </div>
  )
}

const BADGE_COLOUR: Record<string, string> = {
  neutral: C.ink2,
  quiet: C.ink3,
  good: C.keep,
  warn: C.check,
  bad: C.trash,
  accent: C.accent,
}

/**
 * A small integer with an off state, on one 300 px line.
 *
 * `Field` is the right Steam component for this: label on the left, control on the
 * right, one row. The two steps sit either side of the value so Steam's geometric
 * focus resolves left/right onto them without anyone writing a key handler
 * (SPEC §6.1).
 */
export const Stepper: StepperComponent = ({
  label: name,
  value,
  onChange,
  min = 0,
  max = 3,
  offLabel = 'any',
  note,
  clearable = true,
}) => {
  const step = (delta: number) => {
    if (value === null) {
      onChange(delta > 0 ? min : null)
      return
    }
    const next = value + delta
    if (next < min) {
      onChange(clearable ? null : min)
      return
    }
    onChange(Math.min(max, next))
  }
  const button = (glyph: string, delta: number, disabled: boolean, aria: string) => (
    <DialogButton
      noFocusRing
      disabled={disabled}
      aria-label={aria}
      onClick={disabled ? undefined : () => step(delta)}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: 20,
        minWidth: 20,
        height: 20,
        padding: 0,
        borderRadius: 3,
        border: `1px solid ${C.line}`,
        background: C.panel3,
        color: C.ink,
        fontSize: 11,
        opacity: disabled ? 0.4 : 1,
      }}
    >
      {glyph}
    </DialogButton>
  )
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <Field
        label={<span style={label}>{name}</span>}
        padding="compact"
        bottomSeparator="none"
        childrenLayout="inline"
        childrenContainerWidth="min"
      >
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {button('−', -1, value === null, `fewer ${name}`)}
          <span style={{ ...tnum, fontSize: 10, minWidth: 34, textAlign: 'center' }}>
            {value === null ? offLabel : `≥ ${value}`}
          </span>
          {button('+', 1, value !== null && value >= max, `more ${name}`)}
        </span>
      </Field>
      {note ? <span style={{ fontSize: 8.5, color: C.ink3 }}>{note}</span> : null}
    </div>
  )
}

const _implementation: KitImplementation = {
  Screen,
  CheckList,
  Stepper,
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
