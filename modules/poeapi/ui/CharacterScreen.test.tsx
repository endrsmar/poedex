/**
 * The character picker, against a fixture the real backend models produced.
 *
 * `fixtures/character-selection.json` is written by `scripts/make_ui_fixtures.py`
 * through `CharacterSelection.to_json`, and a Python test fails when it drifts — so
 * these assertions are against a payload the backend can actually send.
 *
 * The same file runs under both profiles (`pnpm run test` and `pnpm run test:compact`),
 * which is the point: this screen is the only way to change the character on a Deck,
 * so "it renders at 300 px" is not a nice-to-have to check later. Nothing here names
 * a profile, reaches for a class name, or touches a network.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { clearClient, installClient } from '@poedex/core'
import type { CharacterSelection, PoedexClient } from '@poedex/core'
import { CharacterScreen } from './CharacterScreen'
import { nextPin, playedAgo, reasonOf, toOptions, toneOf } from './model'
import fixture from './fixtures/character-selection.json'

const SELECTION = fixture as unknown as CharacterSelection
const RECENT = 'PlaceholderWarden'
const ANOTHER = 'PlaceholderHierophant'
const NOW = new Date('2026-08-12T12:00:00Z')

/** A selection with nothing readable: the state the report was written about. */
const GUESSED: CharacterSelection = {
  ...SELECTION,
  choice: { ...SELECTION.choice, name: 'PlaceholderJuggernaut', source: 'fallback' },
  characters: (SELECTION.characters ?? []).map((c) => ({ ...c, last_login: null })),
}

const PINNED: CharacterSelection = {
  ...SELECTION,
  choice: {
    ...SELECTION.choice,
    name: ANOTHER,
    source: 'setting',
    league: 'Allflame',
    played_last: RECENT,
  },
  configured: ANOTHER,
}

interface Harness {
  client: PoedexClient
  setCharacter: ReturnType<typeof vi.fn>
}

function harness(first: CharacterSelection = SELECTION, after = PINNED): Harness {
  const setCharacter = vi.fn(async (name: string | null) =>
    name === null ? SELECTION : { ...after, configured: name },
  )
  const client = {
    transport: {
      id: 'http',
      state: 'open',
      call: async () => undefined,
      on: () => () => {},
      connect: () => {},
      close: () => {},
      onStateChange: () => () => {},
    },
    poeapi: {
      characterChoice: vi.fn(async () => first),
      setCharacter,
    },
  } as unknown as PoedexClient
  installClient(client)
  return { client, setCharacter }
}

afterEach(() => {
  cleanup()
  clearClient()
})

/**
 * The picker's own list, by its accessible name.
 *
 * Every name on this screen appears three times — the header `Stat`, the subtitle,
 * and the row — so a bare `getByText` is ambiguous by construction, and scoping is
 * how a query stays honest about which of the three it means.
 */
const list = () => screen.getByRole('group', { name: 'characters to choose from' })

describe('the character picker', () => {
  it('lists every character with its league', async () => {
    harness()
    render(<CharacterScreen />)
    await screen.findByRole('group', { name: 'characters to choose from' })
    for (const name of [RECENT, ANOTHER, 'PlaceholderJuggernaut']) {
      expect(within(list()).getByText(name)).toBeTruthy()
    }
    // The league is the disambiguator, and it is the badge — the one optional part
    // `compact` keeps. Three characters differing only by league is the ordinary
    // case, and reading the parked one is what this whole change is about.
    for (const league of ['Standard', 'Allflame', 'Solo Self-Found']) {
      expect(within(list()).getAllByText(new RegExp(league)).length).toBeGreaterThan(0)
    }
  })

  it('says which character is being read and why, never the name alone', async () => {
    harness()
    render(<CharacterScreen />)
    expect((await screen.findAllByText(RECENT)).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/most recently played/).length).toBeGreaterThan(0)
  })

  it('says it guessed when nothing on the account says', async () => {
    harness(GUESSED)
    render(<CharacterScreen />)
    expect((await screen.findAllByText(/GUESSED/)).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/nothing on this account says/i).length).toBeGreaterThan(0)
  })

  it('does not call a real answer a guess', async () => {
    harness()
    render(<CharacterScreen />)
    await screen.findAllByText(RECENT)
    expect(screen.queryByText(/GUESSED/)).toBeNull()
  })

  it('persists the pick through the backend, not optimistically', async () => {
    const { setCharacter } = harness()
    render(<CharacterScreen />)
    await screen.findByRole('group', { name: 'characters to choose from' })
    await userEvent.click(within(list()).getByText(ANOTHER))
    expect(setCharacter).toHaveBeenCalledWith(ANOTHER)
    // What is rendered afterwards is the backend's answer: `set_character` refuses a
    // name the roster does not have, so an optimistic pin would show a declined one.
    await waitFor(() => expect(screen.getAllByText(/you picked this one/).length).toBeGreaterThan(0))
  })

  it('offers a way back to following the account, and only when there is a pin', async () => {
    harness(PINNED)
    render(<CharacterScreen />)
    expect(await screen.findByText('Follow the account')).toBeTruthy()

    cleanup()
    clearClient()
    harness(SELECTION)
    render(<CharacterScreen />)
    await screen.findAllByText(RECENT)
    expect(screen.queryByText('Follow the account')).toBeNull()
  })

  it('clears the pin by pressing the row that holds it', async () => {
    const { setCharacter } = harness(PINNED)
    render(<CharacterScreen />)
    await screen.findByRole('group', { name: 'characters to choose from' })
    await userEvent.click(within(list()).getByText(ANOTHER))
    expect(setCharacter).toHaveBeenCalledWith(null)
  })

  it('reports a refused name instead of pretending it took', async () => {
    const client = harness().client as unknown as {
      poeapi: { setCharacter: ReturnType<typeof vi.fn> }
    }
    client.poeapi.setCharacter = vi.fn(async () => {
      throw new Error("no character called 'Ghost' on this account")
    })
    render(<CharacterScreen />)
    await screen.findByRole('group', { name: 'characters to choose from' })
    await userEvent.click(within(list()).getByText(ANOTHER))
    expect(await screen.findByText(/no character called/)).toBeTruthy()
  })

  it('says so rather than drawing an empty list when the roster is empty', async () => {
    harness({ ...SELECTION, characters: [], choice: { ...SELECTION.choice, name: null, source: 'none' } })
    render(<CharacterScreen />)
    expect((await screen.findAllByText(/no characters/i)).length).toBeGreaterThan(0)
  })
})

describe('the picker’s model', () => {
  it('reserves warn for a guess and for nothing else', () => {
    expect(toneOf(SELECTION)).toBe('neutral')
    expect(toneOf(PINNED)).toBe('neutral')
    expect(toneOf(GUESSED)).toBe('warn')
  })

  it('reports a pin that disagrees with the account without calling it a fault', () => {
    expect(reasonOf(PINNED)).toContain('you picked this one')
    expect(reasonOf(PINNED)).toContain(`you last played ${RECENT}`)
  })

  it('puts the league in the badge, which is what compact keeps', () => {
    const options = toOptions(SELECTION, NOW)
    expect(options.map((option) => option.badge)).toEqual([
      'Solo Self-Found',
      'Standard',
      'Allflame',
    ])
    expect(options.map((option) => option.id)).toContain(RECENT)
  })

  it('says when each character was last played in words a player can check', () => {
    expect(playedAgo('2026-08-11T21:40:00Z', NOW)).toBe('yesterday')
    expect(playedAgo(null, NOW)).toBe('never played')
    expect(playedAgo('2026-05-02T18:20:00Z', NOW)).toMatch(/months ago/)
  })

  it('toggles a pin off and any other row on', () => {
    expect(nextPin(PINNED, ANOTHER)).toBeNull()
    expect(nextPin(PINNED, RECENT)).toBe(RECENT)
    expect(nextPin(SELECTION, RECENT)).toBe(RECENT)
  })
})
