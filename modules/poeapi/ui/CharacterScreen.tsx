/**
 * The character picker: *which character is this, and how do I read a different one?*
 *
 * Two things this screen is for, and they are not the same thing.
 *
 * **1. Saying which character, and on what grounds.** The tool used to print a name
 * — in the panel header, in `poedex sync`, in the appraise header — with no way to
 * tell "the character you are playing" from "the first name the API happened to
 * return". Against the live account, `current` is never set out of game and the old
 * default fell through to `characters[0]`, so a parked Standard character was read
 * for weeks while the player was in a league. Every claim on this screen therefore
 * carries its rule, and a guess is the one state drawn in `warn`.
 *
 * **2. Overriding it.** Reading a character other than the one you last played is a
 * legitimate thing to want — checking what is parked on a mule, pricing a bag you
 * left on a Standard character — and until now the panel had no way to express it.
 * This is an override, not a workaround for a broken default: the default is right
 * now, and this screen is how you say "not that one". On a Deck it is also the *only*
 * way, because the plugin's settings live under `DECKY_PLUGIN_SETTINGS_DIR` rather
 * than `~/.config/poedex` and there is no usable CLI inside the plugin tree.
 *
 * Built from `CheckList`, which means it is D-pad navigable and keyboard-free
 * without this file writing a focus handler or naming a profile — §2.3 puts
 * navigation in the kit. The roster comes from `get-characters`, cached for an hour,
 * so opening this screen costs nothing on the second visit.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import {
  Action,
  CheckList,
  Empty,
  ErrorState,
  Pending,
  Screen,
  Section,
  Stack,
  StaleBanner,
  Stat,
} from '@poedex/ui'
import { getClient } from '@poedex/core'
import type { CharacterSelection } from '@poedex/core'
import { nextPin, reasonOf, toOptions, toneOf } from './model'

export function CharacterScreen(): ReactElement {
  const client = getClient()
  const [selection, setSelection] = useState<CharacterSelection | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const mounted = useRef(true)

  const load = useCallback(
    async (refresh = false) => {
      try {
        const next = await client.poeapi.characterChoice(refresh)
        if (mounted.current) {
          setSelection(next)
          setError(null)
        }
      } catch (cause: unknown) {
        if (mounted.current) setError(cause instanceof Error ? cause.message : String(cause))
      }
    },
    [client],
  )

  useEffect(() => {
    mounted.current = true
    void load()
    return () => {
      mounted.current = false
    }
  }, [load])

  const pin = useCallback(
    (name: string | null) => {
      setBusy(true)
      setError(null)
      void client.poeapi
        .setCharacter(name)
        // The backend's answer, not the one this screen hoped for. `set_character`
        // refuses a name the roster does not contain, so rendering an optimistic
        // pin would be showing a choice that was declined.
        .then((next) => {
          if (mounted.current) setSelection(next)
        })
        .catch((cause: unknown) => {
          if (mounted.current) setError(cause instanceof Error ? cause.message : String(cause))
        })
        .finally(() => {
          if (mounted.current) setBusy(false)
        })
    },
    [client],
  )

  const toggle = useCallback(
    (id: string) => {
      if (!selection) return
      pin(nextPin(selection, id))
    },
    [pin, selection],
  )

  const banner = (
    <StaleBanner
      status={{
        state: selection?.meta.stale ? 'stale' : selection ? 'fresh' : 'syncing',
        at: selection?.meta.fetched_at ?? null,
        detail: selection?.meta.note ?? null,
        retryAfter: selection?.meta.retry_after ?? null,
      }}
      labels={{
        fresh: 'character list',
        // get-characters is the tightest endpoint on the account and is cached for
        // an hour on purpose, so "stale" here is the normal state and must not read
        // as a fault.
        stale: 'character list, from cache',
      }}
      onRefresh={() => void load(true)}
      refreshLabel="Re-read"
    />
  )

  if (!selection) {
    return (
      <Screen id="character" title="Character" banner={banner}>
        {error ? (
          <ErrorState
            title="could not read the character list"
            detail={error}
            action={<Action label="Try again" onPress={() => void load()} />}
          />
        ) : (
          <Pending label="reading the character list" rows={{ compact: 3, full: 5 }} />
        )}
      </Screen>
    )
  }

  const { choice } = selection
  return (
    <Screen
      id="character"
      title="Character"
      subtitle={choice.name ? `${choice.name} — ${reasonOf(selection)}` : undefined}
      banner={banner}
      actions={
        selection.configured ? (
          <Action
            label="Follow the account"
            hint="Y"
            kind="quiet"
            busy={busy}
            onPress={() => pin(null)}
          />
        ) : undefined
      }
    >
      <Stack gap="lg">
        {error ? (
          <ErrorState
            title="that character was not set"
            detail={error}
            action={<Action label="Try again" onPress={() => void load(true)} />}
          />
        ) : null}

        {/* The one statement this screen exists to make. `warn` is reserved for a
            guess: everything else here is a fact about the account, and a guess is
            the tool admitting it picked. */}
        <Stat
          label="reading"
          value={choice.name ?? 'nothing'}
          note={reasonOf(selection)}
          tone={toneOf(selection)}
          size={{ compact: 'md', full: 'lg' }}
        />

        <Section
          title="characters on this account"
          description={
            selection.configured
              ? 'Pick another to read it instead, or press the one you picked to follow the account again.'
              : 'Pick one to read it instead of the character you last played.'
          }
          meta={`${(selection.characters ?? []).length} characters`}
        >
          {(selection.characters ?? []).length === 0 ? (
            <Empty
              title="no characters"
              detail="This account's roster came back empty. Nothing can be read until it has a character in it."
            />
          ) : (
            <CheckList
              // Not the same words as the section around it: two nested groups with
              // one name is ambiguous to assistive tech and to a test.
              label="characters to choose from"
              options={toOptions(selection)}
              // The tick is *the character being read*, whatever the reason — so
              // the list always agrees with the header above it. Which of them is
              // a standing choice is the `accent` row, and `Follow the account`
              // appears only when there is one to clear.
              selected={choice.name ? [choice.name] : []}
              onToggle={toggle}
              // The league is the badge, and `compact` keeps badges. It is the
              // column that tells a parked character from a played one, which is
              // the distinction the wrong default cost weeks of prices on.
              fields={{ compact: ['badge'], full: ['badge', 'meta'] }}
              emptyLabel="no characters on this account"
            />
          )}
        </Section>
      </Stack>
    </Screen>
  )
}

export default CharacterScreen
