import { useEffect, useState } from 'react'

/**
 * Tick a `Retry-After` down to zero.
 *
 * Profile-agnostic: it is React state and a timer, and both profiles are React.
 * It lives in the kit rather than in a module because "the control is disabled
 * until the limiter will accept a request" is a rule that must hold on every
 * screen, and a rule reimplemented per screen is a rule with an exception.
 *
 * Returns `null` when there is nothing to wait for, so `remaining !== null` is the
 * whole "are we restricted right now" test.
 */
export function useCountdown(seconds: number | null | undefined): number | null {
  const target = seconds === null || seconds === undefined || seconds <= 0 ? null : seconds
  const [remaining, setRemaining] = useState<number | null>(target)

  useEffect(() => {
    setRemaining(target)
    if (target === null) return undefined
    // A deadline rather than a decrementing counter: a background tab throttles
    // timers, and a counter that only decrements when the tab is visible tells the
    // player to wait a minute for something that unblocked forty seconds ago.
    const deadline = Date.now() + target * 1000
    const id = setInterval(() => {
      const left = (deadline - Date.now()) / 1000
      setRemaining(left <= 0 ? null : left)
    }, 250)
    return () => clearInterval(id)
  }, [target])

  return remaining
}
