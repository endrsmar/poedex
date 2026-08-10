import { useCallback, useMemo, useRef } from 'react'
import type {
  GridCellModel,
  ItemGridComponent,
  ItemRowComponent,
  ItemRowField,
  StatComponent,
  TallyComponent,
  ValueBarComponent,
  VerdictPillComponent,
} from '../../contracts'
import { resolveHint } from '../../profile'
import { PROVENANCE_SHORT, VERDICT_GLYPH, VERDICT_LABEL } from '../../verdict'
import { formatPriceCell, formatQuantity, hasPrice } from '../../format'

const P = 'full' as const

const DEFAULT_ROW_FIELDS: ItemRowField[] = [
  'subtitle',
  'quantity',
  'price',
  'provenance',
  'marks',
  'reason',
]

export const Stat: StatComponent = ({
  label,
  value,
  unit,
  prefix,
  secondary,
  note,
  tone = 'neutral',
  size,
}) => (
  <div className="pk-stat" data-size={resolveHint(size, P, 'lg')} data-tone={tone}>
    <span className="pk-label">{label}</span>
    <span className="pk-stat__value">
      {prefix ? (
        <span className="pk-stat__prefix" title="a floor, not a value">
          {prefix}
        </span>
      ) : null}
      {value}
      {unit ? <span className="pk-stat__unit">{unit}</span> : null}
    </span>
    {secondary ? <span className="pk-stat__secondary">{secondary}</span> : null}
    {note ? <span className="pk-stat__note">{note}</span> : null}
  </div>
)

export const Tally: TallyComponent = ({ entries, onSelect, selected }) => (
  <div className="pk-tally" role="group" aria-label="verdict tally">
    {entries.map((entry) => {
      const Element = onSelect ? 'button' : 'div'
      return (
        <Element
          key={entry.id}
          className="pk-tally__cell"
          data-verdict={entry.verdict}
          data-selected={selected === entry.id}
          data-muted={entry.muted ?? entry.count === 0}
          data-pressable={Boolean(onSelect)}
          {...(onSelect
            ? { type: 'button' as const, onClick: () => onSelect(entry.id) }
            : {})}
        >
          <div className="pk-tally__count">{entry.count}</div>
          <div className="pk-label">{entry.label}</div>
        </Element>
      )
    })}
  </div>
)

export const VerdictPill: VerdictPillComponent = ({ verdict, label, count, size }) => (
  <span className="pk-pill" data-verdict={verdict} data-size={resolveHint(size, P, 'sm')}>
    <span aria-hidden="true">{VERDICT_GLYPH[verdict]}</span>
    {count === undefined ? null : <span className="pk-tnum">{count}</span>}
    {label ?? VERDICT_LABEL[verdict]}
  </span>
)

export const ItemGrid: ItemGridComponent = ({
  cells,
  cols,
  rows,
  selected,
  onSelect,
  dimmed = false,
  emptyLabel = 'nothing in the bag',
  label = 'bag layout',
}) => {
  const columns = resolveHint(cols, P, 12)
  const rowCount = resolveHint(rows, P, 5)
  const container = useRef<HTMLDivElement | null>(null)

  const byPosition = useMemo(() => {
    const map = new Map<string, GridCellModel>()
    for (const cell of cells) map.set(`${cell.x},${cell.y}`, cell)
    return map
  }, [cells])

  const covered = useMemo(() => {
    const set = new Set<string>()
    for (const cell of cells) {
      for (let dx = 0; dx < (cell.w ?? 1); dx += 1) {
        for (let dy = 0; dy < (cell.h ?? 1); dy += 1) {
          if (dx || dy) set.add(`${cell.x + dx},${cell.y + dy}`)
        }
      }
    }
    return set
  }, [cells])

  /**
   * Arrow keys move the selection geometrically, which is the same model Steam's
   * focus system gives `compact` for free. Doing it here rather than in the module
   * is §2.3's rule: the module never writes a focus handler.
   */
  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      const deltas: Record<string, [number, number]> = {
        ArrowLeft: [-1, 0],
        ArrowRight: [1, 0],
        ArrowUp: [0, -1],
        ArrowDown: [0, 1],
      }
      const delta = deltas[event.key]
      if (!delta || !onSelect) return
      const current = cells.find((cell) => cell.uid === selected) ?? cells[0]
      if (!current) return
      event.preventDefault()
      let best: GridCellModel | undefined
      let bestScore = Number.POSITIVE_INFINITY
      for (const cell of cells) {
        if (cell.uid === current.uid) continue
        const dx = cell.x - current.x
        const dy = cell.y - current.y
        const along = dx * delta[0] + dy * delta[1]
        if (along <= 0) continue
        const across = Math.abs(dx * delta[1] + dy * delta[0])
        const score = along + across * 12
        if (score < bestScore) {
          bestScore = score
          best = cell
        }
      }
      if (best) {
        onSelect(best.uid)
        const node = container.current?.querySelector<HTMLElement>(`[data-uid="${best.uid}"]`)
        node?.focus()
      }
    },
    [cells, onSelect, selected],
  )

  if (cells.length === 0) {
    return (
      <div className="pk-grid-wrap" data-dimmed={dimmed}>
        <p className="pk-grid__empty">{emptyLabel}</p>
      </div>
    )
  }

  const slots: React.ReactNode[] = []
  for (let y = 0; y < rowCount; y += 1) {
    for (let x = 0; x < columns; x += 1) {
      const key = `${x},${y}`
      if (covered.has(key)) continue
      const cell = byPosition.get(key)
      if (!cell) {
        slots.push(
          <div
            key={key}
            className="pk-cell"
            data-filled="false"
            aria-hidden="true"
            // Explicit, not auto-placed: a multi-slot item makes auto-placement
            // slide every later empty cell, and a grid that is a *map* has to put
            // the empty slots where the empty slots are.
            style={{ gridColumn: `${x + 1}`, gridRow: `${y + 1}` }}
          />,
        )
        continue
      }
      slots.push(
        <button
          key={cell.uid}
          type="button"
          data-uid={cell.uid}
          className="pk-cell"
          data-filled="true"
          data-verdict={cell.verdict}
          data-selected={selected === cell.uid}
          style={{
            gridColumn: `${x + 1} / span ${cell.w ?? 1}`,
            gridRow: `${y + 1} / span ${cell.h ?? 1}`,
          }}
          title={`${cell.label} — ${VERDICT_LABEL[cell.verdict]}`}
          aria-label={`${cell.label}, ${VERDICT_LABEL[cell.verdict]}`}
          aria-pressed={selected === cell.uid}
          onClick={onSelect ? () => onSelect(cell.uid) : undefined}
        >
          <span className="pk-cell__mark" aria-hidden="true">
            {VERDICT_GLYPH[cell.verdict]}
          </span>
          <span className={`pk-cell__glyph pk-rar-${cell.rarity}`}>{cell.glyph}</span>
          {cell.quantity && cell.quantity > 1 ? (
            <span className="pk-cell__qty">{compactQuantity(cell.quantity)}</span>
          ) : null}
        </button>,
      )
    }
  }

  return (
    <div className="pk-grid-wrap" data-dimmed={dimmed}>
      <div
        ref={container}
        className="pk-grid"
        role="grid"
        aria-label={label}
        aria-busy={dimmed}
        // The declared shape, on the element. A quad tab is 24x24 and a bag is
        // 12x5, and "which lattice is this?" is otherwise only inspectable through
        // a style attribute that the two profiles spell differently.
        data-cols={columns}
        data-rows={rowCount}
        onKeyDown={onKeyDown}
        style={{
          gridTemplateColumns: `repeat(${columns}, 1fr)`,
          gridTemplateRows: `repeat(${rowCount}, 1fr)`,
        }}
      >
        {slots}
      </div>
    </div>
  )
}

/** `40296` will not fit in 22 px, so the cell shows `40k` and the row shows all of it. */
function compactQuantity(quantity: number): string {
  if (quantity < 1000) return String(quantity)
  if (quantity < 1_000_000) return `${Math.round(quantity / 1000)}k`
  return `${Math.round(quantity / 1_000_000)}m`
}

export const ItemRow: ItemRowComponent = ({ item, selected = false, onSelect, fields }) => {
  const shown = new Set(resolveHint(fields, P, DEFAULT_ROW_FIELDS))
  const Element = onSelect ? 'button' : 'div'
  const price = item.price
  const priceText = formatPriceCell(price, 1)

  return (
    <Element
      className="pk-itemrow"
      data-verdict={item.verdict}
      data-selected={selected}
      data-pressable={Boolean(onSelect)}
      data-uid={item.uid}
      {...(onSelect ? { type: 'button' as const, onClick: () => onSelect(item.uid) } : {})}
    >
      <span className="pk-itemrow__stripe" aria-hidden="true" />
      <span className="pk-itemrow__glyph" aria-hidden="true">
        {VERDICT_GLYPH[item.verdict]}
      </span>
      <span className="pk-itemrow__body">
        <span className={`pk-itemrow__name pk-rar-${item.rarity}`}>{item.name}</span>
        {shown.has('subtitle') && item.subtitle ? (
          <span className="pk-itemrow__sub">{item.subtitle}</span>
        ) : null}
        {shown.has('marks') && item.marks?.length ? (
          <span className="pk-itemrow__marks">
            {item.marks.map((mark) => (
              <span key={mark.id} className="pk-mark" data-tone={mark.tone} title={mark.detail}>
                {mark.label}
              </span>
            ))}
          </span>
        ) : null}
      </span>
      {shown.has('quantity') ? (
        <span className="pk-itemrow__qty">
          {item.quantity && item.quantity > 1 ? formatQuantity(item.quantity) : ''}
        </span>
      ) : null}
      {shown.has('price') ? (
        <span
          className="pk-itemrow__price"
          data-unpriced={!hasPrice(price)}
          title={price?.detail ?? undefined}
        >
          {priceText}
        </span>
      ) : null}
      {shown.has('provenance') ? (
        <span className="pk-itemrow__prov">
          {hasPrice(price) ? PROVENANCE_SHORT[price.provenance] : ''}
        </span>
      ) : null}
      {shown.has('reason') && item.reason ? (
        <span className="pk-itemrow__reason" title={item.reason}>
          {item.reason}
        </span>
      ) : null}
    </Element>
  )
}

export const ValueBar: ValueBarComponent = ({ bars, max, limit, label }) => {
  const cap = resolveHint(limit, P, null)
  const shown = cap === null ? bars : bars.slice(0, Math.max(0, cap))
  const ceiling = max ?? Math.max(1, ...bars.map((bar) => bar.value))
  return (
    <div className="pk-bars" role="list" aria-label={label}>
      {shown.map((bar) => (
        <div key={bar.id} className="pk-bar" role="listitem">
          {bar.label ? <span className="pk-bar__label">{bar.label}</span> : null}
          <span className="pk-bar__track">
            <span
              className="pk-bar__fill"
              data-tone={bar.tone}
              style={{ width: `${Math.min(100, Math.max(2, (bar.value / ceiling) * 100))}%` }}
            />
          </span>
          <span className="pk-bar__value">{bar.display ?? bar.value}</span>
        </div>
      ))}
      {shown.length < bars.length ? (
        <span className="pk-section__more">{bars.length - shown.length} more not shown</span>
      ) : null}
    </div>
  )
}
