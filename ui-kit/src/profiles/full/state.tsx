import type {
  EmptyComponent,
  ErrorStateComponent,
  PendingComponent,
  StaleBannerComponent,
} from '../../contracts'
import { resolveHint } from '../../profile'
import { useCountdown } from '../../countdown'
import { REFRESH_LABEL, syncMessage } from '../../sync'
import { Action } from './interaction'

const P = 'full' as const

export const Pending: PendingComponent = ({ label = 'loading', rows }) => {
  const count = resolveHint(rows, P, 4)
  return (
    <div className="pk-pending" role="status" aria-live="polite" aria-label={label}>
      {Array.from({ length: count }, (_unused, index) => (
        <div key={index} className="pk-pending__bar" />
      ))}
      <span className="pk-sr">{label}</span>
    </div>
  )
}

export const Empty: EmptyComponent = ({ title, detail, action }) => (
  <div className="pk-empty">
    <span className="pk-empty__title">{title}</span>
    {detail ? <span className="pk-empty__detail">{detail}</span> : null}
    {action}
  </div>
)

export const ErrorState: ErrorStateComponent = ({ title, detail, kind = 'failure', action }) => (
  <div className="pk-errorstate" data-kind={kind} role="alert">
    <span className="pk-errorstate__title">{title}</span>
    {detail ? <span className="pk-errorstate__detail">{detail}</span> : null}
    {action}
  </div>
)

/**
 * Six states, six sentences, one place.
 *
 * The wording is the product here, so it is worth being explicit about what each
 * one refuses to say:
 *
 * * `unchanged` says **"no change since HH:MM"**, never "refreshed". A fetch that
 *   returned identical bytes is not new information, and a surface that celebrates
 *   it teaches the player that the timestamp means nothing.
 * * `stale` says the fetch did not happen. It is not a quieter `fresh`.
 * * `syncing` keeps the previous timestamp visible. The grid dims elsewhere; it
 *   never blanks, so there is always something on screen that is true.
 * * `restricted` shows the live countdown and **disables the control**. The
 *   alternative — accepting the press and dropping it — is the behaviour that makes
 *   a player press again harder.
 */
export const StaleBanner: StaleBannerComponent = ({
  status,
  onRefresh,
  refreshLabel = REFRESH_LABEL,
  labels,
}) => {
  const remaining = useCountdown(status.retryAfter)
  const state = status.state
  const message = syncMessage(status, remaining, labels)

  return (
    <div className="pk-banner" data-state={state} role="status" aria-live="polite">
      <span className="pk-banner__dot" aria-hidden="true" />
      <span>{message}</span>
      {status.detail ? <span className="pk-banner__detail">{status.detail}</span> : null}
      {onRefresh ? (
        <span className="pk-banner__spacer">
          <Action
            label={refreshLabel}
            kind="quiet"
            onPress={onRefresh}
            busy={state === 'syncing'}
            countdown={state === 'restricted' ? status.retryAfter : null}
          />
        </span>
      ) : null}
    </div>
  )
}
