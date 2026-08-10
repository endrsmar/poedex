/**
 * The stash screen: which tabs are worth opening, and what is in the one you opened.
 *
 * Declared `profiles: ['full']`, and that is a decision rather than an omission —
 * IMPLEMENTATION-PLAN §2.4 exists to let a screen say where it makes sense. The
 * reasoning, stated so it can be argued with:
 *
 * * **A 117-row tab list does not fit at 300 px.** The bag screen survives there
 *   because it is 60 cells and a five-row list; a stash list is a hundred rows each
 *   carrying a name, a kind, a shape, an age and a value, and truncating it to five
 *   would hide exactly the tabs the player has not looked at.
 * * **A quad tab is 24x24.** At 300 px that is a 12 px cell before gaps. The bag
 *   grid works at 22 px and research-notes §8 already found *that* tight.
 * * **research-notes §8 says the grid's justification does not transfer.** "When you
 *   are looking at your stash you are standing at it, and the game shows it full
 *   size." The panel's job in front of a stash is a digest, and the digest that
 *   belongs at 300 px is a different screen from this one — a ranked list of *tabs
 *   worth walking to*, not a browser. Shipping this one cramped would be shipping a
 *   worse version of both.
 *
 * What it does *not* do, and this is the load-bearing part: **it never fetches a tab
 * by itself.** The digest is free — the tab list plus whatever is already on disk —
 * and every item request on this screen is behind a press. SPEC §6.6: never
 * auto-crawl.
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
  formatChaos,
} from '@poedex/ui'
import { createStashStore, getClient } from '@poedex/core'
import type { StashDigestPayload, TabAppraisalPayload } from '@poedex/core'
import { PriceCheck } from './PriceCheck'
import {
  blocksOf,
  copyFor,
  costNote,
  describeAge,
  stashNote,
  tabCells,
  tabRow,
  toDetail,
  toRow,
} from './model'

export function StashScreen(): ReactElement {
  const client = getClient()
  const store = useMemo(() => createStashStore({ client }), [client])
  const state = useSyncExternalStore(store.subscribe, store.get, store.get)
  const [selected, setSelected] = useState<string | null>(null)

  useEffect(() => {
    // The *digest*, which spends no item requests. Not a tab: a screen that read a
    // tab on mount would spend the player's budget for having navigated.
    void store.refresh()
    return store.listen()
  }, [store])

  const openTab = useCallback(
    (index: string) => {
      setSelected(null)
      void store.openTab(Number(index))
    },
    [store],
  )

  const { digest, tab, openIndex, sync, loadingTab } = state

  const banner = (
    <StaleBanner
      status={{ state: sync.state, at: sync.at, detail: sync.detail, retryAfter: sync.retryAfter }}
      onRefresh={() => void store.refresh(true)}
      refreshLabel="Re-read tab list"
    />
  )

  if (!digest) {
    return (
      <Screen id="stash" title="Stash" banner={banner}>
        {sync.state === 'error' || sync.state === 'restricted' ? (
          <ErrorState
            title={
              sync.state === 'restricted'
                ? 'the rate limiter refused this request'
                : 'could not read the tab list'
            }
            detail={sync.detail ?? undefined}
            action={
              <Action
                label="Try again"
                onPress={() => void store.refresh(true)}
                countdown={sync.retryAfter}
              />
            }
          />
        ) : (
          <Pending label="reading the tab list" rows={{ compact: 4, full: 10 }} />
        )}
      </Screen>
    )
  }

  return (
    <Screen
      id="stash"
      title="Stash"
      subtitle={`${digest.league} · ${digest.tab_count} tabs · gate: ${digest.strictness}`}
      banner={banner}
      actions={
        tab ? (
          <Action label="Back to tabs" kind="quiet" onPress={() => store.closeTab()} />
        ) : undefined
      }
      aside={
        <Stack gap="md">
          <Detail
            item={tab && selected ? detailFor(tab, selected) : null}
            fields={{
              compact: ['name', 'value', 'provenance', 'reason', 'gate'],
              full: ['name', 'value', 'provenance', 'stack', 'location', 'reason', 'gate'],
            }}
            empty="open a tab and pick an item to see where its number came from"
          />
          {/* The bag's own PriceCheck, with a tab index. Same component, same
              checkbox list, same MAX_PRETICKED — Phase 10 extends the check to the
              stash by not forking it. */}
          <PriceCheck
            uid={tab ? checkableUid(tab, selected) : null}
            tabIndex={tab ? tab.tab.index : null}
            divineRate={digest.divine_rate}
          />
        </Stack>
      }
    >
      <Stack gap="lg">
        <Row gap="lg" align="end" wrap={{ compact: true, full: false }}>
          <StashTotal digest={digest} />
          <Stat
            label="to refresh"
            value={`${digest.cost.requests}`}
            unit="req"
            note={costNote(digest)}
            tone={digest.cost.requests > 20 ? 'warn' : 'quiet'}
            size="sm"
          />
          <Stat
            label="read"
            value={`${digest.known_count}/${digest.tab_count}`}
            note={`${digest.cost.permanent_tabs} tab(s) are remove-only: fetched once, correct forever`}
            size="sm"
          />
        </Row>

        <Section
          title="tabs"
          meta={`${digest.tab_count} tabs · ${digest.highlighted_count} rows to check`}
          description="Opening a tab costs one request. Nothing here fetches on its own."
          limit={{ compact: 5, full: null }}
          limitNoun="tabs"
        >
          {digest.tabs.map((entry) => (
            <ItemRow
              key={entry.index}
              item={tabRow(entry)}
              selected={openIndex === entry.index}
              onSelect={openTab}
              fields={{
                compact: ['subtitle', 'quantity', 'price', 'marks'],
                full: ['subtitle', 'quantity', 'price', 'provenance', 'marks', 'reason'],
              }}
            />
          ))}
        </Section>

        {loadingTab ? <Pending label="reading the tab" rows={{ compact: 3, full: 6 }} /> : null}

        {tab ? <OpenTab tab={tab} selected={selected} onSelect={setSelected} /> : null}
      </Stack>
    </Screen>
  )
}

function StashTotal({ digest }: { digest: StashDigestPayload }): ReactElement {
  const note = stashNote(digest)
  return (
    <Stat
      label="stash value"
      value={formatChaos(digest.total_chaos)}
      unit="c"
      // `≥` whenever a tab is unread or unreadable. The alternative is a total that
      // silently counts an unopened tab as nothing, which is the one thing this
      // screen must never do.
      prefix={digest.total_is_floor ? '≥' : undefined}
      secondary={
        digest.total_divine !== null && digest.total_divine !== undefined
          ? `${digest.total_divine.toFixed(2)} div`
          : undefined
      }
      note={note ?? undefined}
      tone={note ? 'warn' : 'neutral'}
      size="lg"
    />
  )
}

function OpenTab({
  tab,
  selected,
  onSelect,
}: {
  tab: TabAppraisalPayload
  selected: string | null
  onSelect: (uid: string) => void
}): ReactElement {
  if (tab.unsupported) {
    // Not an empty tab and not an error: a thing this build cannot read, said in
    // full. A zero here would be a confident wrong answer.
    return (
      <ErrorState
        kind="unavailable"
        title={`${tab.tab.name || `tab ${tab.tab.index}`} — not supported yet`}
        detail={tab.unsupported}
      />
    )
  }

  const cells = tabCells(tab)
  const shape = tab.tab.grid ? `${tab.tab.cols}x${tab.tab.rows}` : 'special layout'
  return (
    <Stack gap="md">
      <Section
        title={`${tab.tab.name || `tab ${tab.tab.index}`} · ${tab.tab.kind}`}
        meta={`${tab.items.length} rows · ${shape} · read ${describeAge(tab.tab.age_seconds)}`}
        description={
          tab.tab.grid
            ? 'Slot-accurate: a cell is where the item actually sits.'
            : // Said rather than silently listed. A currency tab has a bespoke layout
              // and `stackSize` legitimately exceeds `maxStackSize` there, so an
              // invented lattice would be a picture that looks right and is not.
              'This tab has no grid — special tabs place items in bespoke slots, so they are listed rather than drawn.'
        }
      >
        {tab.tab.grid ? (
          <ItemGrid
            cells={cells}
            cols={tab.tab.cols ?? 12}
            rows={tab.tab.rows ?? 12}
            selected={selected}
            onSelect={onSelect}
            emptyLabel="this tab is empty"
            label={`${tab.tab.name || 'tab'} layout, by verdict`}
          />
        ) : null}
      </Section>

      {tab.items.length === 0 ? (
        <Empty title="nothing in this tab" detail="It was read, and it is empty." />
      ) : null}

      {blocksOf(tab).map((block) => {
        if (block.items.length === 0) return null
        const copy = copyFor(block)
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
            limit={{ compact: 5, full: null }}
          >
            {block.items.map((entry) => (
              <ItemRow
                key={entry.uid}
                item={toRow(entry, null)}
                selected={selected === entry.uid}
                onSelect={onSelect}
                fields={{
                  compact: ['subtitle', 'quantity', 'price', 'provenance', 'marks'],
                  full: ['subtitle', 'quantity', 'price', 'provenance', 'marks', 'reason'],
                }}
              />
            ))}
          </Section>
        )
      })}
    </Stack>
  )
}

/**
 * The selected uid, but only where asking the market is a real question.
 *
 * The same rule the bag screen follows: a highlighted row with no price is what the
 * checkbox list is for, and offering to spend two trade requests on a stack of
 * Chaos Orbs would be offering a worse answer than the one already on screen.
 */
function checkableUid(tab: TabAppraisalPayload, uid: string | null): string | null {
  if (!uid) return null
  const found = tab.items.find((item) => item.uid === uid)
  return found && found.highlighted && found.valuation.unpriceable ? uid : null
}

function detailFor(tab: TabAppraisalPayload, uid: string) {
  const found = tab.items.find((item) => item.uid === uid)
  return found ? toDetail(found, null) : null
}

export default StashScreen
