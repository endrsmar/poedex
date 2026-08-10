/**
 * The manual price check: a checkbox list, an open-affix option, and a button.
 *
 * IMPLEMENTATION-PLAN §5b. The gate no longer prices a rare; it says the item is
 * *worth asking about*, and this is where the player answers. What the panel is
 * careful about is exactly what the two failed live runs got wrong:
 *
 * * **The tier is shown where `moddb` asserted one, and the word `unknown` where it
 *   did not.** About one affix line in five is unattributable — several groups can
 *   render one sentence, and their tier numbers are counted from different ladders.
 *   A row reading `T2` when the truth is "probably T2, possibly T3" is a lie the
 *   player acts on and cannot check, so those rows say so and are not pre-ticked.
 * * **The result replaces the pending state, and never a previous answer silently.**
 *   A check in flight shows `Pending`; a finished one shows its number *with the
 *   comparable count beside it*, because a median over one listing is one stranger's
 *   asking price and that is what reported 10c for a 1c jewel.
 * * **Nothing is truncated.** `CheckList` takes no `limit`: a hidden row is a filter
 *   the player cannot see and cannot switch off.
 *
 * One component, both surfaces. The 300 px layout lives in the kit's `compact`
 * implementation of `CheckList` — two lines per row, tier under the mod text, a
 * 30 px focus target — declared here only as a `fields` hint. No `overrides/`.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactElement } from 'react'
import {
  Action,
  CheckList,
  ErrorState,
  Pending,
  Row,
  Section,
  Stack,
  Stat,
  Stepper,
} from '@poedex/ui'
import { getClient } from '@poedex/core'
import type { ItemHighlightPayload, PriceCheckPayload } from '@poedex/core'
import { checkNote, optionsOf, priceLine, ticksOf, unsearchableNote } from './model'

export interface PriceCheckProps {
  /** The selected item, or `null`. `null` renders the invitation, not an error. */
  uid: string | null
  /** Chaos per divine, from the bag payload. The backend publishes no rate. */
  divineRate?: number | null
  /**
   * Which stash tab the item is in, when it is in one. Omitted, the uid is looked
   * for in the bag.
   *
   * This is the whole of Phase 10's change to this component. An item does not
   * become a different question because of where it is sitting, so the checkbox
   * list, the pre-tick cap and the query are untouched — only *where to find the
   * uid* differs, and it is one argument.
   */
  tabIndex?: number | null
}

type Phase = 'idle' | 'loading' | 'checking' | 'failed'

export function PriceCheck({ uid, divineRate, tabIndex }: PriceCheckProps): ReactElement | null {
  const client = getClient()
  const [highlight, setHighlight] = useState<ItemHighlightPayload | null>(null)
  const [ticks, setTicks] = useState<string[]>([])
  const [openPrefixes, setOpenPrefixes] = useState<number | null>(null)
  const [openSuffixes, setOpenSuffixes] = useState<number | null>(null)
  const [result, setResult] = useState<PriceCheckPayload | null>(null)
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!uid) {
      setHighlight(null)
      setResult(null)
      setPhase('idle')
      return
    }
    let live = true
    setPhase('loading')
    setResult(null)
    setError(null)
    // A new item means a new question. Carrying the previous item's ticks over
    // would put filters the player chose for something else into this query.
    setOpenPrefixes(null)
    setOpenSuffixes(null)
    void client.appraisal
      .highlight(uid, null, tabIndex ?? null)
      .then((payload) => {
        if (!live) return
        setHighlight(payload)
        setTicks(ticksOf(payload))
        setPhase('idle')
      })
      .catch((problem: unknown) => {
        if (!live) return
        setError(problem instanceof Error ? problem.message : String(problem))
        setPhase('failed')
      })
    return () => {
      live = false
    }
  }, [client, uid, tabIndex])

  const toggle = useCallback((id: string) => {
    setTicks((current) =>
      current.includes(id) ? current.filter((entry) => entry !== id) : [...current, id],
    )
  }, [])

  const options = useMemo(() => (highlight ? optionsOf(highlight) : []), [highlight])
  const unsearchable = useMemo(
    () => (highlight ? unsearchableNote(highlight, ticks) : null),
    [highlight, ticks],
  )
  const asksNothing = ticks.length === 0 && openPrefixes === null && openSuffixes === null

  const check = useCallback(() => {
    if (!uid || asksNothing) return
    setPhase('checking')
    setError(null)
    void client.appraisal
      .priceCheck(uid, {
        mods: ticks.map((id) => Number(id)),
        open_prefixes: openPrefixes,
        open_suffixes: openSuffixes,
        tab_index: tabIndex ?? null,
      })
      .then((payload) => {
        setResult(payload)
        setPhase('idle')
      })
      .catch((problem: unknown) => {
        setError(problem instanceof Error ? problem.message : String(problem))
        setPhase('failed')
      })
  }, [asksNothing, client, openPrefixes, openSuffixes, tabIndex, ticks, uid])

  if (!uid) return null

  if (phase === 'loading') {
    return (
      <Section title="price check">
        <Pending label="reading the mods" rows={{ compact: 3, full: 5 }} />
      </Section>
    )
  }

  if (phase === 'failed' || !highlight) {
    return (
      <Section title="price check">
        <ErrorState
          title="could not read this item"
          detail={error ?? 'the item is no longer where it was'}
        />
      </Section>
    )
  }

  return (
    <Section
      title="price check"
      meta={highlight.highlighted ? 'worth asking about' : 'not highlighted'}
      description="Tick the mods that make this item interesting. The search is built from your ticks and nothing else."
      tone={highlight.highlighted ? 'warn' : 'neutral'}
    >
      <Stack gap="sm">
        <CheckList
          options={options}
          selected={ticks}
          onToggle={toggle}
          label={`mods on ${highlight.name}`}
          // At 300 px the tier stays and the affix/ceiling line goes: the tier is
          // the whole reason this list is worth reading, and `prefix · 130 of 144`
          // is context the panel can afford only when there is room.
          fields={{ compact: ['badge'], full: ['badge', 'meta'] }}
          emptyLabel="no readable mods on this item"
        />

        {unsearchable ? (
          // Said before the button, not after the answer. `build_plan` already reports
          // what it dropped, but it reports it in the query description — which arrives
          // once the trade requests have been spent. A tick that cannot become a filter
          // is a question the player will not get an answer to, and they should find
          // that out while it still costs a tick to fix.
          <Stat
            label="not searchable"
            value={String(unsearchable.split(' ')[0])}
            note={unsearchable}
            tone="warn"
            size="sm"
          />
        ) : null}

        <Row gap="md" wrap={{ compact: true, full: false }} align="end">
          <Stepper
            label="open prefixes"
            value={openPrefixes}
            onChange={setOpenPrefixes}
            max={highlight.max_prefixes}
            offLabel="any"
            // The counts are a floor when a line's side could not be decided, and a
            // filter built on a wrong one returns nothing and looks like a dead
            // search. Whoever builds it has to be told.
            note={
              highlight.counts_are_certain
                ? `${highlight.open_prefixes} free on this item`
                : 'affix counts uncertain — this filter may be wrong'
            }
            tone={highlight.counts_are_certain ? 'neutral' : 'warn'}
          />
          <Stepper
            label="open suffixes"
            value={openSuffixes}
            onChange={setOpenSuffixes}
            max={highlight.max_suffixes}
            offLabel="any"
            note={
              highlight.counts_are_certain
                ? `${highlight.open_suffixes} free on this item`
                : 'affix counts uncertain — this filter may be wrong'
            }
            tone={highlight.counts_are_certain ? 'neutral' : 'warn'}
          />
        </Row>

        <Row gap="sm" justify="between" wrap={{ compact: true, full: false }}>
          <Action
            label="Check price"
            kind="primary"
            hint="A"
            busy={phase === 'checking'}
            disabled={asksNothing}
            onPress={check}
          />
          <Action label="Reset ticks" kind="quiet" onPress={() => setTicks(ticksOf(highlight))} />
        </Row>

        {asksNothing ? (
          <Stat
            label="nothing selected"
            value="—"
            note="A search with no filters prices the cheapest item sharing this base, which is not this item's price."
            tone="quiet"
            size="sm"
          />
        ) : null}

        {phase === 'checking' ? (
          <Pending label="asking the trade API" rows={{ compact: 1, full: 2 }} />
        ) : null}

        {result && phase !== 'checking' ? (
          <Stat
            label={result.priced ? 'price' : 'no price'}
            value={priceLine(result)}
            unit={result.priced ? 'c' : undefined}
            secondary={
              result.priced && divineRate && result.chaos
                ? `${(result.chaos / divineRate).toFixed(2)} div`
                : undefined
            }
            // The comparable count is in the note, always, and it is why the note is
            // not optional decoration: `10.0c` over one listing and `10.0c` over 438
            // are different claims wearing the same number.
            note={checkNote(result)}
            tone={result.priced && !result.thin ? 'good' : 'warn'}
            size={{ compact: 'md', full: 'lg' }}
          />
        ) : null}

        <Stat
          label="mod database"
          value={`${highlight.mods.length} lines`}
          note={highlight.note}
          tone="quiet"
          size="sm"
        />
      </Stack>
    </Section>
  )
}

export default PriceCheck
