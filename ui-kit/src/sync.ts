/**
 * The words for the six sync states, in the kit rather than in a profile.
 *
 * They started out duplicated in `full` and `compact`, and the two copies had
 * already drifted before anything shipped: one said *"cached — nothing was
 * fetched"* and the other said *"cached"*; one called the control **Refresh** and
 * the other **Resync**. That is exactly the failure mode surface profiles are
 * supposed to prevent — a difference in *wording* is a difference in what the tool
 * claims, not a difference in density.
 *
 * So the sentence is built once. What a profile still decides is how much of it
 * fits and what the control looks like.
 */

import type { SyncState, SyncStatus } from './contracts/model'
import { formatAge, formatClock, formatCountdown } from './format'

export const SYNC_LABELS: Record<SyncState, string> = {
  fresh: 'synced',
  stale: 'cached — nothing was fetched',
  syncing: 'syncing…',
  unchanged: 'no change',
  error: 'sync failed',
  restricted: 'rate limited',
}

export const REFRESH_LABEL = 'Refresh'

/**
 * `remaining` is the live countdown, or `null` when nothing is being waited on.
 * It is passed in rather than read here so the timer lives in one hook.
 */
export function syncMessage(
  status: SyncStatus,
  remaining: number | null,
  labels?: Partial<Record<SyncState, string>>,
): string {
  const base = labels?.[status.state] ?? SYNC_LABELS[status.state]
  switch (status.state) {
    case 'fresh':
      return status.at ? `${base} ${formatAge(status.at)} ago` : base
    // "no change since 14:32", never "refreshed". A fetch that returned what it
    // already had is not new information, and a surface that celebrates it teaches
    // the player that the timestamp means nothing.
    case 'unchanged':
      return status.at ? `${base} since ${formatClock(status.at)}` : base
    case 'stale':
      return status.at ? `${base} · from ${formatClock(status.at)}` : base
    case 'restricted':
      return remaining !== null ? `${base} — retry in ${formatCountdown(remaining)}` : base
    default:
      return base
  }
}

/** The footer a truncated `Section` shows. Same words at both densities. */
export function hiddenLabel(hidden: number, noun: string, pressable: boolean): string {
  // "1 more items" is the kind of thing that makes a tool look unfinished on the
  // one row where it is trying to say it has hidden something from you.
  const word = hidden === 1 ? noun.replace(/s$/, '') : noun
  return pressable ? `show ${hidden} more ${word}` : `${hidden} more ${word} not shown`
}
