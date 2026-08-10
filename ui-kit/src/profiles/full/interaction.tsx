import type { ActionComponent, DetailComponent, DetailField } from '../../contracts'
import { resolveHint } from '../../profile'
import { useCountdown } from '../../countdown'
import { formatCountdown, formatQuantity } from '../../format'
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
            {price?.pricing
              ? '⋯'
              : price && price.chaos !== null && price.chaos !== undefined
                ? `${price.chaos.toLocaleString('en-US', { maximumFractionDigits: 2 })}c`
                : '—'}
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
