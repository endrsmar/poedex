/**
 * The bag screen: *is any of this worth a stash trip, or is it all vendor trash?*
 *
 * One component, both surfaces. Every density decision on this screen is a `Hint`,
 * so the 300 px panel and the 1400 px page are the same tree with different numbers
 * in it — no `useProfile()`, no branch, no `overrides/compact.tsx`.
 *
 * What the screen is built to get right, all of it from live data rather than from
 * a design exercise:
 *
 * * **Four verdicts, and `unpriceable` is one of them.** It has its own block, its
 *   own colour and its own tally slot even when the count is zero.
 * * **The total is a floor and says so.** `≥` is a field on `Stat`, not string
 *   concatenation, and the sentence under it names both holes separately: rows the
 *   index does not carry, and rows still being priced.
 * * **Provenance is part of the answer.** Every row shows which claim its number
 *   rests on — poe.ninja, the bulk exchange, a trade search, or the player's own
 *   `~price` note.
 * * **League provenance too.** The subtitle reads "Allflame (from the character)"
 *   or shouts when something overrode it. A silently wrong league was a real bug.
 * * **`check` is drawn as two blocks.** See `checkLane` in `model.ts` for why.
 * * **A highlighted rare gets a checkbox list, not a price.** Selecting one puts
 *   `PriceCheck` in the aside: its mods with real per-base tiers, the significant
 *   ones pre-ticked, an open-affix option, and a button. Nothing here asks the trade
 *   API on its own — IMPLEMENTATION-PLAN §5b.
 * * **Five-digit stacks fit.** Quantity and price are fixed-width tabular columns.
 * * **Two rows with the same name stay two rows.** Nothing here merges by name.
 */

import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react'
import type { ReactElement } from 'react'
import {
  Action,
  Detail,
  Empty,
  ErrorState,
  ItemGrid,
  ItemRow,
  Pending,
  Row,
  Screen,
  Section,
  Stack,
  StaleBanner,
  Stat,
  Tally,
  formatChaos,
} from '@poedex/ui'
import type { GridCellModel } from '@poedex/ui'
import { createBagStore, getClient } from '@poedex/core'
import type { BagAppraisalPayload } from '@poedex/core'
import { PriceCheck } from './PriceCheck'
import {
  blocksOf,
  copyFor,
  floorNote,
  tallyOf,
  toDetail,
  toGridCell,
  toRow,
  totalsOf,
} from './model'

/** 12×5 is the backpack. Both profiles draw the same shape; only the cell size differs. */
const BAG_COLS = 12
const BAG_ROWS = 5

export function BagScreen(): ReactElement {
  const client = getClient()
  const store = useMemo(() => createBagStore({ client }), [client])
  const state = useSyncExternalStore(store.subscribe, store.get, store.get)
  const [selected, setSelected] = useState<string | null>(null)
  const [showAllTrash, setShowAllTrash] = useState(false)

  useEffect(() => {
    void store.refresh()
    return store.listen()
  }, [store])

  const refresh = useCallback(() => {
    void store.refresh()
  }, [store])

  const { bag, sync } = state

  const banner = (
    <StaleBanner
      status={{
        state: sync.state,
        at: sync.at,
        detail: sync.detail,
        retryAfter: sync.retryAfter,
      }}
      onRefresh={refresh}
      refreshLabel="Refresh"
    />
  )

  if (!bag) {
    return (
      <Screen id="bag" title="Bag" banner={banner}>
        {sync.state === 'error' || sync.state === 'restricted' ? (
          <ErrorState
            title={
              sync.state === 'restricted'
                ? 'the rate limiter refused this request'
                : 'could not read the bag'
            }
            detail={sync.detail ?? undefined}
            action={<Action label="Try again" onPress={refresh} countdown={sync.retryAfter} />}
          />
        ) : (
          <Pending label="reading the bag" rows={{ compact: 4, full: 8 }} />
        )}
      </Screen>
    )
  }

  return (
    <Screen
      id="bag"
      title="Bag"
      subtitle={describeContext(bag)}
      banner={banner}
      actions={
        <Action
          label="Re-price"
          kind="quiet"
          hint="X"
          onPress={() => {
            void client.prices.refresh(true, bag.league).then(refresh)
          }}
        />
      }
      aside={
        <Stack gap="md">
          <Detail
            item={detailFor(bag, selected)}
            fields={{
              compact: ['name', 'value', 'provenance', 'reason', 'gate', 'comparables'],
              full: [
                'name',
                'value',
                'provenance',
                'stack',
                'location',
                'reason',
                'gate',
                'comparables',
              ],
            }}
            empty="pick an item to see where its number came from"
          />
          {/* Only for a row the highlighter flagged. Drawing a checkbox list beside
              a Divine Orb would offer the player a question with no answer in it. */}
          <PriceCheck uid={checkableUid(bag, selected)} divineRate={bag.divine_rate} />
        </Stack>
      }
    >
      <Stack gap="lg">
        <Row gap="lg" align="end" wrap={{ compact: true, full: false }}>
          <BagTotal bag={bag} />
          <VerdictTally bag={bag} />
        </Row>

        <Section title="bag layout" meta={`${bag.items.length} rows`}>
          <ItemGrid
            cells={gridCells(bag)}
            cols={BAG_COLS}
            rows={BAG_ROWS}
            selected={selected}
            onSelect={setSelected}
            // `syncing` dims the grid and never blanks it: a grid that disappears
            // during a refresh is a grid the player stops trusting to be there.
            dimmed={sync.state === 'syncing'}
            emptyLabel="the bag is empty"
            label="bag layout, by verdict"
          />
        </Section>

        {bag.items.length === 0 ? (
          <Empty
            title="nothing in the backpack"
            detail="Pick something up and come back, or check that the right character is selected."
          />
        ) : null}

        {blocksOf(bag).map((block) => {
          if (block.items.length === 0) return null
          const copy = copyFor(block)
          const isTrash = block.verdict === 'trash'
          // A money subtotal on the not-loot block would restate the very question
          // the block exists to retire, so it counts rows like the unpriced ones do.
          const unpriced =
            block.verdict === 'unpriceable' ||
            block.verdict === 'not_loot' ||
            block.lane === 'unknown'
          return (
            <Section
              key={`${block.verdict}-${block.lane ?? 'all'}`}
              title={copy.title}
              meta={
                unpriced
                  ? `${block.items.length} rows · ${block.units.toLocaleString('en-US')} units`
                  : `${block.items.length} items · ${formatChaos(block.subtotal)}c`
              }
              description={copy.headline}
              tone={block.verdict === 'check' ? 'warn' : 'neutral'}
              // Trash is the largest block on a real bag and the least informative.
              // It is collapsed rather than hidden, and the count is always shown —
              // nothing is hidden without saying how much.
              // 3 is the CLI's own collapse threshold (`cli/appraise.py`), kept so the
              // two renderers agree about when a trash block stops being worth reading.
              limit={isTrash && !showAllTrash ? { compact: 0, full: 3 } : { compact: 5, full: null }}
              limitNoun={unpriced ? 'rows' : 'items'}
              onShowAll={isTrash ? () => setShowAllTrash(true) : undefined}
              collapsible={{ compact: isTrash, full: false }}
              defaultCollapsed={{ compact: isTrash, full: false }}
            >
              {block.items.map((entry) => (
                <ItemRow
                  key={entry.uid}
                  item={toRow(entry, bag.divine_rate)}
                  selected={selected === entry.uid}
                  onSelect={setSelected}
                  // `provenance` and `marks` survive at 300 px. Provenance is four
                  // characters and it is part of the answer; the marks carry the
                  // gate's reasoning, which on an unpriced `check` row is the only
                  // content there is. What `compact` gives up is the reason column.
                  fields={{
                    compact: ['subtitle', 'quantity', 'price', 'provenance', 'marks'],
                    full: ['subtitle', 'quantity', 'price', 'provenance', 'marks', 'reason'],
                  }}
                />
              ))}
            </Section>
          )
        })}

        <Provenance bag={bag} />
      </Stack>
    </Screen>
  )
}

function BagTotal({ bag }: { bag: BagAppraisalPayload }): ReactElement {
  const totals = totalsOf(bag)
  const note = floorNote(bag)
  return (
    <Stat
      label="bag value"
      value={totals.chaos}
      unit="c"
      // SPEC §5.3: `≥ N` while anything is outstanding or missing. A field, so the
      // 300 px profile can set it as a superscript instead of spending a character.
      prefix={totals.isFloor ? '≥' : undefined}
      secondary={totals.divine ?? undefined}
      note={note ?? undefined}
      tone={note ? 'warn' : 'neutral'}
      size={{ compact: 'lg', full: 'lg' }}
    />
  )
}

function VerdictTally({ bag }: { bag: BagAppraisalPayload }): ReactElement {
  return <Tally entries={tallyOf(bag)} />
}

/**
 * The six facts a verdict list is only meaningful against.
 *
 * Lifted from `cli/appraise.py`'s header, because the CLI renderer is the current
 * best expression of what this screen has to say — and because a panel that shows
 * numbers without showing which league and which tables produced them is asking to
 * be trusted rather than checked.
 */
function Provenance({ bag }: { bag: BagAppraisalPayload }): ReactElement {
  const table = bag.table
  return (
    <Section
      title="how this was decided"
      description="Every figure above is denominated in one league's economy, from tables of a known age."
      collapsible={{ compact: true, full: false }}
      defaultCollapsed={{ compact: true, full: false }}
    >
      <Row gap="lg" wrap={true} align="start">
        <Stat
          size="sm"
          label="league"
          value={bag.league}
          note={
            bag.league_overridden
              ? 'OVERRIDE — this is not the character’s own league'
              : 'from the character'
          }
          tone={bag.league_overridden ? 'warn' : 'neutral'}
        />
        <Stat
          size="sm"
          label="character"
          value={bag.character ?? '—'}
          // The same discipline as the league beside it, and for a larger reason:
          // this line read `character: PlaceholderWarden` for weeks while nobody had chosen
          // PlaceholderWarden, and it looked exactly as it looks when somebody has.
          note={characterNote(bag)}
          tone={bag.character_source === 'fallback' ? 'warn' : 'neutral'}
        />
        <Stat
          size="sm"
          label="keep at"
          value={`${formatChaos(bag.threshold_chaos)}c`}
          note={`gate: ${bag.strictness}`}
        />
        <Stat
          size="sm"
          label="price tables"
          value={table ? `${table.loaded}/${table.requested}` : '—'}
          note={
            table
              ? [table.stale ? 'STALE' : 'fresh', table.discovery, table.note]
                  .filter(Boolean)
                  .join(' · ')
              : 'no table status'
          }
          tone={table?.stale ? 'warn' : 'neutral'}
        />
        <Stat
          size="sm"
          label="lookups"
          value={String(bag.lookups)}
          note={`${bag.trade_requests} trade request${bag.trade_requests === 1 ? '' : 's'} spent`}
        />
      </Row>
    </Section>
  )
}

/**
 * "Allflame (from the character)" — never a bare league name.
 *
 * `LeagueChoice.describe()` says the same thing in the CLI, and it says it because
 * pricing an Allflame bag against Standard is a legitimate thing to ask for and an
 * illegitimate thing to do by accident. The only difference visible from outside is
 * whether it was announced.
 */
function describeContext(bag: BagAppraisalPayload): string {
  const league = bag.league_overridden
    ? `${bag.league} (OVERRIDE — not the character’s league)`
    : `${bag.league} (from the character)`
  const note = characterNote(bag)
  const character = bag.character ? `${bag.character} (${note})` : null
  return [character, league].filter(Boolean).join(' · ')
}

/**
 * Which character, and on what grounds — never the name alone.
 *
 * `fallback` is the state this exists for. It means no character was marked as being
 * played and none carried a `lastLoginTime`, so the name above it is the first entry
 * GGG returned and nothing more. Everything else here is an observation or a choice;
 * that one is the tool saying it picked, and a header that renders it like the
 * others is asserting a guess.
 */
function characterNote(bag: BagAppraisalPayload): string {
  const base = CHARACTER_REASON[bag.character_source ?? 'none']
  if (bag.character_played_last && bag.character_played_last !== bag.character) {
    return `${base} · you last played ${bag.character_played_last}`
  }
  return base
}

const CHARACTER_REASON: Record<NonNullable<BagAppraisalPayload['character_source']>, string> = {
  argument: 'named on the command line',
  environment: 'set by POEDEX_CHARACTER',
  setting: 'you picked this one',
  current: 'the character you are playing',
  last_login: 'most recently played',
  fallback: 'GUESSED — nothing said which character to read; pick one on the Character screen',
  none: 'no character',
}

function gridCells(bag: BagAppraisalPayload): GridCellModel[] {
  const cells: GridCellModel[] = []
  for (const item of bag.items) {
    const cell = toGridCell(item)
    if (cell) cells.push(cell)
  }
  return cells
}

/**
 * The selected uid, but only when asking about it is a real question.
 *
 * A highlighted row with no price is exactly the case §5b built the checkbox list
 * for. Everything else already has a number from a bulk table, and offering to spend
 * two trade requests on a Divine Orb would be offering a worse answer than the one
 * already on screen.
 */
function checkableUid(bag: BagAppraisalPayload, uid: string | null): string | null {
  if (!uid) return null
  const found = bag.items.find((item) => item.uid === uid)
  return found && found.highlighted && found.valuation.unpriceable ? uid : null
}

function detailFor(bag: BagAppraisalPayload, uid: string | null) {
  if (!uid) return null
  const found = bag.items.find((item) => item.uid === uid)
  return found ? toDetail(found, bag.divine_rate) : null
}

export default BagScreen
