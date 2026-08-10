import type {
  ActionComponent,
  CheckField,
  CheckListComponent,
  DetailComponent,
  DetailField,
  StepperComponent,
} from '../../contracts'
import { resolveHint } from '../../profile'
import { useCountdown } from '../../countdown'
import { formatCountdown, formatPriceCell, formatQuantity } from '../../format'
import { PROVENANCE_LABEL, VERDICT_GLYPH, VERDICT_HEADLINE } from '../../verdict'
import { ValueBar, VerdictPill } from './data'

const P = 'full' as const

export const Action: ActionComponent = ({
  label,
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
    <button
      type="button"
      className="pk-action"
      data-kind={kind}
      disabled={blocked}
      aria-busy={busy}
      onClick={blocked ? undefined : onPress}
    >
      {hint ? (
        <span className="pk-action__hint" aria-hidden="true">
          {hint}
        </span>
      ) : null}
      {label}
      {remaining !== null ? (
        <span className="pk-action__countdown">{formatCountdown(remaining)}</span>
      ) : null}
    </button>
  )
}

export const Detail: DetailComponent = ({ item, fields, empty = 'nothing selected', actions }) => {
  const shown = resolveHint<DetailField[]>(fields, P, ['name', 'value'])
  if (!item) {
    return (
      <div className="pk-detail" role="group" aria-label="item detail">
        <p className="pk-detail__empty">{empty}</p>
      </div>
    )
  }
  const price = item.price
  return (
    <div className="pk-detail" role="group" aria-label="item detail">
      {shown.includes('name') ? (
        <div>
          <div className={`pk-detail__name pk-rar-${item.rarity}`}>{item.name}</div>
          {item.subtitle ? <div className="pk-detail__sub">{item.subtitle}</div> : null}
          <div style={{ marginTop: '0.4rem' }}>
            <VerdictPill verdict={item.verdict} size="md" />{' '}
            <span className="pk-muted" style={{ fontSize: '0.78rem' }}>
              {VERDICT_HEADLINE[item.verdict]}
            </span>
          </div>
        </div>
      ) : null}

      {shown.includes('value') ? (
        <div className="pk-detail__block">
          <span className="pk-label">value</span>
          <span className="pk-stat__value" style={{ fontSize: '1.35rem' }}>
            {formatPriceCell(price, 2)}
          </span>
          {shown.includes('provenance') && price ? (
            <span className="pk-muted" style={{ fontSize: '0.76rem' }}>
              {PROVENANCE_LABEL[price.provenance]}
              {price.detail ? ` · ${price.detail}` : ''}
            </span>
          ) : null}
        </div>
      ) : null}

      {shown.includes('stack') && item.quantity && item.quantity > 1 ? (
        <div className="pk-detail__block">
          <span className="pk-label">stack</span>
          <span className="pk-tnum">{formatQuantity(item.quantity)}</span>
        </div>
      ) : null}

      {shown.includes('location') && item.location ? (
        <div className="pk-detail__block">
          <span className="pk-label">where</span>
          <span className="pk-tnum">{item.location}</span>
        </div>
      ) : null}

      {shown.includes('reason') && item.reason ? (
        <div className="pk-detail__block">
          <span className="pk-label">why</span>
          <span style={{ fontSize: '0.82rem' }}>
            <span aria-hidden="true">{VERDICT_GLYPH[item.verdict]} </span>
            {item.reason}
          </span>
        </div>
      ) : null}

      {shown.includes('gate') && item.gate ? (
        <div className="pk-detail__block">
          <span className="pk-label">tier-2 gate</span>
          {item.gate.considered ? (
            item.gate.signals.length ? (
              <span className="pk-itemrow__marks">
                {item.gate.signals.map((signal) => (
                  <span
                    key={signal.id}
                    className="pk-mark"
                    data-tone={signal.tone}
                    title={signal.detail}
                  >
                    {signal.label}
                  </span>
                ))}
              </span>
            ) : (
              <span className="pk-muted" style={{ fontSize: '0.8rem' }}>
                considered, nothing flagged
              </span>
            )
          ) : (
            <span className="pk-muted" style={{ fontSize: '0.8rem' }}>
              not considered — bulk items never enter tier 2
            </span>
          )}
        </div>
      ) : null}

      {shown.includes('mods') && item.mods?.length ? (
        <div className="pk-detail__block">
          <span className="pk-label">mods</span>
          <div className="pk-detail__mods">
            {item.mods.map((group) =>
              group.lines.map((line, index) => (
                <span
                  key={`${group.group}-${index}`}
                  className="pk-detail__mod"
                  data-group={group.group}
                >
                  {line}
                </span>
              )),
            )}
          </div>
        </div>
      ) : null}

      {shown.includes('comparables') && item.comparables?.length ? (
        <div className="pk-detail__block">
          <span className="pk-label">comparable listings</span>
          <ValueBar bars={item.comparables} label="comparable listings" />
        </div>
      ) : null}

      {actions ? <div className="pk-detail__actions">{actions}</div> : null}
    </div>
  )
}

/**
 * The checkbox list (Phase 9).
 *
 * At `full` a row is one line: box, mod text, then the tier badge and the meta in a
 * right-aligned column. There is room, so nothing is dropped.
 *
 * A disabled row stays **visible and legible**. A mod with no trade filter is still
 * a mod on the item, and hiding it would make the list disagree with the tooltip in
 * game — which is the one comparison a player can actually make.
 */
export const CheckList: CheckListComponent = ({
  options,
  selected,
  onToggle,
  label = 'mods to search on',
  fields,
  emptyLabel = 'no readable mods',
}) => {
  const shown = new Set(resolveHint<CheckField[]>(fields, P, ['badge', 'meta']))
  const ticked = new Set(selected)
  if (options.length === 0) {
    return (
      <div className="pk-checklist" role="group" aria-label={label}>
        <p className="pk-detail__empty">{emptyLabel}</p>
      </div>
    )
  }
  return (
    <div className="pk-checklist" role="group" aria-label={label}>
      {options.map((option) => (
        <label
          key={option.id}
          className="pk-check"
          // The same hook `compact` sets, so a test can find a row without knowing
          // which profile drew it. `ItemRow` has carried `data-uid` since Phase 5
          // for exactly this reason.
          data-check-id={option.id}
          data-tone={option.tone ?? 'neutral'}
          data-disabled={option.disabled ? 'true' : undefined}
          title={option.disabled ? option.disabledReason : undefined}
        >
          <input
            type="checkbox"
            className="pk-check__box"
            checked={ticked.has(option.id)}
            disabled={option.disabled}
            // Named explicitly rather than inherited from the label, so the two
            // profiles expose the *same* accessible name: the mod line, and the
            // reason it is off when it is. The badge is presentation.
            aria-label={
              option.disabled && option.disabledReason
                ? `${option.label} — ${option.disabledReason}`
                : option.label
            }
            onChange={() => onToggle(option.id)}
          />
          <span className="pk-check__label">{option.label}</span>
          {shown.has('badge') && option.badge ? (
            <span className="pk-check__badge">{option.badge}</span>
          ) : null}
          {shown.has('meta') && option.meta ? (
            <span className="pk-check__meta">{option.meta}</span>
          ) : null}
        </label>
      ))}
    </div>
  )
}

/**
 * A small integer with an off state (Phase 9).
 *
 * `null` is not zero and the control says so: "at least 0 open prefixes" matches
 * every item in the league, while `null` leaves the filter out of the query
 * altogether. Two different searches, so two different displayed states.
 */
export const Stepper: StepperComponent = ({
  label,
  value,
  onChange,
  min = 0,
  max = 3,
  offLabel = 'any',
  note,
  tone = 'neutral',
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
  return (
    <div className="pk-stepper" data-tone={tone}>
      <span className="pk-label">{label}</span>
      <span className="pk-stepper__controls">
        <button
          type="button"
          className="pk-stepper__step"
          aria-label={`fewer ${label}`}
          disabled={value === null}
          onClick={() => step(-1)}
        >
          −
        </button>
        <span className="pk-stepper__value">{value === null ? offLabel : `≥ ${value}`}</span>
        <button
          type="button"
          className="pk-stepper__step"
          aria-label={`more ${label}`}
          disabled={value !== null && value >= max}
          onClick={() => step(1)}
        >
          +
        </button>
      </span>
      {note ? <span className="pk-stat__note">{note}</span> : null}
    </div>
  )
}
