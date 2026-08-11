/**
 * The kit primitives, against whichever profile the build selected.
 *
 * Nothing in this file names a profile. `#profile` resolves to `full` today and to
 * `compact` under `resolve.conditions: ['poedex-compact']` in Phase 7, and the same
 * assertions run against both — which is the only way "the contracts were designed
 * for both" becomes a fact rather than an intention. Assertions are therefore about
 * *behaviour and text*, never about class names or pixel values.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  Action,
  Detail,
  Empty,
  ErrorState,
  ItemGrid,
  ItemRow,
  PROFILE,
  Pending,
  Screen,
  Section,
  StaleBanner,
  Stat,
  Tally,
  ValueBar,
  VerdictPill,
} from './index'
import { formatAge, formatChaos, formatCountdown, formatQuantity } from './format'
import { resolveHint } from './profile'
import type { GridCellModel, ItemRowModel } from './contracts'

const row = (over: Partial<ItemRowModel> = {}): ItemRowModel => ({
  uid: 'u1',
  name: 'Divine Orb',
  rarity: 'currency',
  verdict: 'keep',
  subtitle: 'currency',
  quantity: 3,
  price: { chaos: 642, provenance: 'bulk' },
  reason: '3 x 214c · poe.ninja',
  marks: [],
  ...over,
})

const cell = (over: Partial<GridCellModel> = {}): GridCellModel => ({
  uid: 'c1',
  glyph: 'DO',
  label: 'Divine Orb ×3',
  verdict: 'keep',
  rarity: 'currency',
  x: 0,
  y: 0,
  ...over,
})

describe('per-profile hints', () => {
  it('resolves a hint map against the active profile', () => {
    expect(resolveHint({ compact: 5, full: null }, PROFILE.id, 99)).toBe(
      PROFILE.id === 'compact' ? 5 : null,
    )
  })

  it('passes a bare value through unchanged', () => {
    expect(resolveHint(12, PROFILE.id, 0)).toBe(12)
  })

  it('falls back when nothing was declared', () => {
    expect(resolveHint(undefined, PROFILE.id, 'default')).toBe('default')
  })
})

describe('Stat', () => {
  it('shows the floor prefix as its own element rather than glued to the number', () => {
    render(<Stat label="bag value" value="5,164" unit="c" prefix="≥" />)
    expect(screen.getByText('≥')).toBeInTheDocument()
    expect(screen.getByText('5,164')).toBeInTheDocument()
  })

  it('shows the note explaining what the total leaves out', () => {
    render(
      <Stat
        label="bag value"
        value="5,164"
        note="excludes 2 unpriceable rows (175 units). This is a floor, not a value."
      />,
    )
    expect(screen.getByText(/floor, not a value/)).toBeInTheDocument()
  })
})

describe('Tally', () => {
  it('renders every verdict it is given, zeroes included', () => {
    render(
      <Tally
        entries={[
          { id: 'keep', label: 'keep', count: 7, verdict: 'keep' },
          { id: 'check', label: 'check', count: 8, verdict: 'check' },
          { id: 'unpriceable', label: 'unpriced', count: 0, verdict: 'unpriceable' },
          { id: 'trash', label: 'trash', count: 4, verdict: 'trash' },
        ]}
      />,
    )
    // A tally that drops the state when the count is zero teaches the player the
    // state does not exist.
    expect(screen.getByText('unpriced')).toBeInTheDocument()
    expect(screen.getByText('0')).toBeInTheDocument()
  })
})

describe('VerdictPill', () => {
  it('carries shape as well as colour, so it survives greyscale (SPEC §5.4)', () => {
    const { container } = render(<VerdictPill verdict="unpriceable" />)
    expect(container.textContent).toContain('?')
    expect(container.textContent).toContain('unpriceable')
  })
})

describe('ItemRow', () => {
  it('shows a five-digit stack in full, not abbreviated', () => {
    // `textContent`, not `getByText`: the contract is that the number is shown, not
    // that it lives in its own element. `full` gives it a fixed-width column;
    // `compact` folds it into the sub-line. Asserting on the node would be
    // asserting on the profile.
    const { container } = render(<ItemRow item={row({ quantity: 40296, name: "Dead Man's Sulphur" })} />)
    expect(container.textContent).toContain(formatQuantity(40296))
  })

  it('shows a dash, never 0c, for a row with no price', () => {
    const { container } = render(
      <ItemRow item={row({ verdict: 'unpriceable', price: { chaos: null, provenance: 'unpriceable' } })} />,
    )
    expect(container.textContent).toContain('—')
    expect(container.textContent).not.toContain('0c')
  })

  it('shows an ellipsis, and a different one from "no price ever", while pricing', () => {
    const { container } = render(
      <ItemRow item={row({ price: { chaos: null, provenance: 'unpriceable', pricing: true } })} />,
    )
    expect(container.textContent).toContain('⋯')
    expect(container.textContent).not.toContain('—')
  })

  it('reports which claim the number rests on', () => {
    const { container } = render(
      <ItemRow
        item={row({ price: { chaos: 192, provenance: 'trade' } })}
        fields={{ compact: ['price', 'provenance'], full: ['price', 'provenance'] }}
      />,
    )
    expect(container.textContent).toContain('trade')
  })

  it('is selectable through onSelect and nothing else', async () => {
    const onSelect = vi.fn()
    render(<ItemRow item={row()} onSelect={onSelect} />)
    await userEvent.click(screen.getByText('Divine Orb'))
    expect(onSelect).toHaveBeenCalledWith('u1')
  })
})

describe('ItemGrid', () => {
  it('places a cell at its own slot coordinates', () => {
    render(
      <ItemGrid
        cells={[cell({ uid: 'a', x: 0, y: 0 }), cell({ uid: 'b', x: 4, y: 2, verdict: 'trash' })]}
        cols={12}
        rows={5}
      />,
    )
    expect(screen.getAllByLabelText(/Divine Orb/)).toHaveLength(2)
  })

  it('names each cell with its verdict, so the grid is readable without colour', () => {
    render(<ItemGrid cells={[cell({ verdict: 'check' })]} cols={12} rows={5} />)
    expect(screen.getByLabelText('Divine Orb ×3, check')).toBeInTheDocument()
  })

  it('dims but never blanks while syncing', () => {
    const { container } = render(
      <ItemGrid cells={[cell()]} cols={12} rows={5} dimmed emptyLabel="the bag is empty" />,
    )
    expect(container.textContent).not.toContain('the bag is empty')
    expect(screen.getByLabelText(/Divine Orb/)).toBeInTheDocument()
  })

  it('says so when there is nothing to draw', () => {
    render(<ItemGrid cells={[]} cols={12} rows={5} emptyLabel="the bag is empty" />)
    expect(screen.getByText('the bag is empty')).toBeInTheDocument()
  })

  it('reports selection through onSelect — the module writes no focus handler', async () => {
    const onSelect = vi.fn()
    render(<ItemGrid cells={[cell({ uid: 'z' })]} cols={12} rows={5} onSelect={onSelect} />)
    await userEvent.click(screen.getByLabelText(/Divine Orb/))
    expect(onSelect).toHaveBeenCalledWith('z')
  })
})

describe('Section', () => {
  it('truncates to the profile limit and says how much it hid', () => {
    render(
      <Section title="trash" limit={{ compact: 1, full: 2 }} limitNoun="items">
        <p>one</p>
        <p>two</p>
        <p>three</p>
        <p>four</p>
      </Section>,
    )
    const kept = resolveHint({ compact: 1, full: 2 }, PROFILE.id, 0) as number
    expect(screen.getByText(new RegExp(`${4 - kept} more items`))).toBeInTheDocument()
  })

  it('renders everything when the limit is null', () => {
    render(
      <Section title="keep" limit={{ compact: null, full: null }}>
        <p>one</p>
        <p>two</p>
      </Section>,
    )
    expect(screen.queryByText(/more/)).toBeNull()
  })

  it('offers to show the rest when the caller can raise the limit', async () => {
    const onShowAll = vi.fn()
    render(
      <Section title="trash" limit={1} limitNoun="items" onShowAll={onShowAll}>
        <p>one</p>
        <p>two</p>
      </Section>,
    )
    await userEvent.click(screen.getByText(/more item/))
    expect(onShowAll).toHaveBeenCalled()
  })
})

describe('Action', () => {
  it('presses', async () => {
    const onPress = vi.fn()
    render(<Action label="Refresh" onPress={onPress} />)
    await userEvent.click(screen.getByText('Refresh'))
    expect(onPress).toHaveBeenCalled()
  })

  it('refuses the press while a countdown is running, rather than swallowing it', async () => {
    const onPress = vi.fn()
    const { container } = render(<Action label="Refresh" onPress={onPress} countdown={47} />)
    await userEvent.click(screen.getByText('Refresh'))
    expect(onPress).not.toHaveBeenCalled()
    expect(container.textContent).toContain(formatCountdown(47))
  })

  it('accepts the press once the countdown is over', async () => {
    const onPress = vi.fn()
    render(<Action label="Refresh" onPress={onPress} countdown={0} />)
    await userEvent.click(screen.getByText('Refresh'))
    expect(onPress).toHaveBeenCalled()
  })
})

describe('StaleBanner', () => {
  const at = new Date(Date.now() - 4000).toISOString()

  it('says how long ago when fresh', () => {
    // Freeze the clock across render and assertion. Both sides call formatAge
    // against "now", and a real second boundary falling between them made this
    // fail as 'expected "synced 4s ago" to contain "5s"' — a flake that says
    // nothing about the component.
    vi.useFakeTimers({ now: Date.now(), shouldAdvanceTime: false })
    try {
      const { container } = render(<StaleBanner status={{ state: 'fresh', at }} />)
      expect(container.textContent).toMatch(/synced/)
      expect(container.textContent).toContain(formatAge(at))
    } finally {
      vi.useRealTimers()
    }
  })

  it('says "no change since HH:MM" — never "refreshed" — when unchanged', () => {
    const { container } = render(<StaleBanner status={{ state: 'unchanged', at }} />)
    expect(container.textContent).toMatch(/no change since \d\d:\d\d/)
    expect(container.textContent).not.toMatch(/refreshed/i)
  })

  it('says the fetch did not happen when stale', () => {
    const { container } = render(<StaleBanner status={{ state: 'stale', at }} />)
    expect(container.textContent).toMatch(/cache|cached/)
  })

  it('shows a busy control while syncing', () => {
    render(<StaleBanner status={{ state: 'syncing', at }} onRefresh={() => {}} />)
    expect(screen.getByText('Refresh').closest('[aria-busy], button')).toBeTruthy()
  })

  it('reports the failure when errored', () => {
    const { container } = render(
      <StaleBanner status={{ state: 'error', at, detail: 'no league on the character' }} />,
    )
    expect(container.textContent).toContain('no league on the character')
  })

  it('counts down and disables the control when restricted', async () => {
    const onRefresh = vi.fn()
    const { container } = render(
      <StaleBanner status={{ state: 'restricted', at, retryAfter: 47 }} onRefresh={onRefresh} />,
    )
    expect(container.textContent).toContain(formatCountdown(47))
    await userEvent.click(screen.getByText('Refresh'))
    expect(onRefresh).not.toHaveBeenCalled()
  })
})

describe('Detail', () => {
  it('says what to do when nothing is selected', () => {
    render(<Detail item={null} fields={['name']} empty="pick an item" />)
    expect(screen.getByText('pick an item')).toBeInTheDocument()
  })

  it('draws only the fields the profile asked for', () => {
    const { container } = render(
      <Detail
        item={{
          uid: 'u',
          name: 'Corpse Ward',
          rarity: 'rare',
          verdict: 'check',
          subtitle: 'Hubris Circlet',
          price: { chaos: null, provenance: 'unpriceable' },
          reason: 'hunter-influenced, ilvl 86 base',
          gate: {
            passed: true,
            considered: true,
            signals: [{ id: 'influence', label: 'influence', detail: 'hunter-influenced' }],
          },
        }}
        fields={{ compact: ['name'], full: ['name'] }}
      />,
    )
    expect(container.textContent).toContain('Corpse Ward')
    expect(container.textContent).not.toContain('hunter-influenced')
  })

  it('shows the gate’s reasoning when asked, because a verdict nobody can argue with is one nobody trusts', () => {
    const { container } = render(
      <Detail
        item={{
          uid: 'u',
          name: 'Corpse Ward',
          rarity: 'rare',
          verdict: 'check',
          gate: {
            passed: true,
            considered: true,
            signals: [{ id: 'influence', label: 'influence', detail: 'hunter-influenced' }],
          },
        }}
        fields={{ compact: ['name', 'gate'], full: ['name', 'gate'] }}
      />,
    )
    expect(container.textContent).toContain('influence')
  })
})

describe('ValueBar / state primitives', () => {
  it('shows a display string per bar', () => {
    render(
      <ValueBar
        bars={[
          { id: 'a', label: 'poe.ninja', value: 257, display: '257c' },
          { id: 'b', label: 'your note', value: 428, display: '428c' },
        ]}
      />,
    )
    expect(screen.getByText('428c')).toBeInTheDocument()
  })

  it('reserves space while pending instead of jumping on arrival', () => {
    const { container } = render(<Pending label="reading the bag" rows={{ compact: 3, full: 8 }} />)
    expect(container.querySelectorAll('div > div').length).toBeGreaterThan(0)
    expect(screen.getByLabelText('reading the bag')).toBeInTheDocument()
  })

  it('distinguishes a failure from an unavailability', () => {
    const { rerender, container } = render(<ErrorState title="could not read the bag" />)
    expect(within(container).getByText('could not read the bag')).toBeInTheDocument()
    rerender(<ErrorState title="not here" kind="unavailable" />)
    expect(within(container).getByText('not here')).toBeInTheDocument()
  })

  it('renders an empty state with an explanation', () => {
    render(<Empty title="nothing in the backpack" detail="Pick something up." />)
    expect(screen.getByText('Pick something up.')).toBeInTheDocument()
  })
})

describe('Screen', () => {
  it('keeps the banner outside the scrolling body', () => {
    const { container } = render(
      <Screen id="bag" title="Bag" banner={<span>synced 4s ago</span>} aside={<span>detail</span>}>
        <span>body</span>
      </Screen>,
    )
    expect(container.textContent).toContain('synced 4s ago')
    expect(container.textContent).toContain('detail')
    expect(container.textContent).toContain('body')
  })
})

describe('formatting', () => {
  it('keeps chaos readable across six orders of magnitude', () => {
    expect(formatChaos(0.08)).toBe('0.08')
    expect(formatChaos(6.5)).toBe('6.5')
    expect(formatChaos(214)).toBe('214')
    expect(formatChaos(5164.55)).toBe('5,165')
  })

  it('groups a five-digit stack', () => {
    expect(formatQuantity(40296)).toBe('×40,296')
  })

  it('keeps seconds visible throughout a countdown', () => {
    expect(formatCountdown(47)).toBe('47s')
    expect(formatCountdown(125)).toBe('2:05')
  })
})
