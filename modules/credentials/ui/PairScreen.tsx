/**
 * Pairing: how a 32-character credential gets onto a Deck with no keyboard.
 *
 * SPEC §4.1, and the reason it exists is worth restating because the screen looks
 * trivial: POESESSID is a 32-character hex string, and gaming mode offers a
 * thumbstick-driven on-screen keyboard with no paste-in and no copy. Typing it is
 * 60-120 seconds with a real typo rate on a string where one wrong character is
 * indistinguishable from an expired session. So **nothing is typed on the Deck**.
 * The Deck shows an address and six digits; the PC — where the cookie already is —
 * does the pasting.
 *
 * One component, both surfaces, like every other screen here. It is `['compact',
 * 'full']` rather than compact-only because the pairing flow is not a small-screen
 * workaround: a desktop user who does not want to open a terminal for
 * `poedex auth set` wants exactly this, and running it through both profiles is how
 * the harness keeps the compact one honest.
 *
 * What the screen is built to get right:
 *
 * * **The credential never comes near it.** There is no text field on this screen
 *   and no method that could carry a value: `pair_start`, `pair_status` and
 *   `pair_cancel`, and none of them takes one. What is on screen is a code and a
 *   URL.
 * * **A live countdown, because the window really does close.** A code that expired
 *   silently is a player typing a correct code into a page that says nothing.
 * * **Every ending has its own sentence.** Paired, expired, cancelled, and *refused*
 *   — three wrong codes — are four different things to have happened, and "pairing
 *   failed" for all of them is how somebody retries the thing that will not work.
 * * **The address is not assumed to exist.** A Deck on no network has no RFC1918
 *   address to show, and saying so beats printing `http://:7332`.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import {
  Action,
  Empty,
  ErrorState,
  Row,
  Screen,
  Section,
  Stack,
  Stat,
  StaleBanner,
} from '@poedex/ui'
import { getClient } from '@poedex/core'
import type { CredentialStatusPayload, PairingStatusPayload } from '@poedex/core'

const IDLE: PairingStatusPayload = {
  state: 'idle',
  code: null,
  port: null,
  urls: [],
  expires_in: null,
  attempts_left: null,
  detail: null,
}

/** What the credential state means, in the words a player can act on. */
const CREDENTIAL_TEXT: Record<CredentialStatusPayload['state'], string> = {
  never_set: 'No session stored. Pair to add one.',
  set: 'Stored, but no request has used it yet.',
  ok: 'Accepted by the API.',
  rejected: 'The API rejected it — it expired or was revoked. Pair again.',
}

/** Every way a window can end, and the one sentence each of them earns. */
const ENDING_TEXT: Record<string, string> = {
  paired: 'Paired. The session is stored on the Deck.',
  expired: 'The window timed out. Nothing was stored.',
  cancelled: 'Pairing cancelled.',
  refused: 'Too many wrong codes, so the window closed. Start another one.',
}

export function PairScreen(): ReactElement {
  const client = getClient()
  const [pairing, setPairing] = useState<PairingStatusPayload>(IDLE)
  const [credential, setCredential] = useState<CredentialStatusPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const mounted = useRef(true)

  const readCredential = useCallback(async () => {
    const status = await client.credentials.status()
    if (mounted.current) setCredential(status)
  }, [client])

  useEffect(() => {
    mounted.current = true
    void readCredential()
    void client.credentials.pairStatus().then((status) => {
      if (mounted.current) setPairing(status)
    })
    // Both topics, because they answer different questions: `pairing_changed` is
    // the window, `credential_changed` is what is on disk. A window that expired
    // unpaired changes one and not the other.
    const off = [
      client.transport.on('pairing_changed', (event) => {
        if (mounted.current) setPairing(event.payload as unknown as PairingStatusPayload)
      }),
      client.transport.on('credential_changed', () => {
        void readCredential()
      }),
    ]
    return () => {
      mounted.current = false
      for (const stop of off) stop()
    }
  }, [client, readCredential])

  const start = useCallback(() => {
    setError(null)
    setBusy(true)
    void client.credentials
      .pairStart()
      .then((status) => {
        if (mounted.current) setPairing(status)
      })
      .catch((cause: unknown) => {
        if (mounted.current) setError(cause instanceof Error ? cause.message : String(cause))
      })
      .finally(() => {
        if (mounted.current) setBusy(false)
      })
  }, [client])

  const cancel = useCallback(() => {
    void client.credentials.pairCancel().then((status) => {
      if (mounted.current) setPairing(status)
    })
  }, [client])

  const waiting = pairing.state === 'waiting'
  const banner = useMemo(
    () => (
      <StaleBanner
        status={{
          state: credential?.usable ? 'fresh' : 'stale',
          at: credential?.last_ok_at ?? credential?.added_at ?? null,
          detail: credential ? CREDENTIAL_TEXT[credential.state] : 'reading credential state',
          retryAfter: null,
        }}
        labels={{
          fresh: credential?.account ? `session for ${credential.account}` : 'session stored',
          stale: 'no usable session',
        }}
      />
    ),
    [credential],
  )

  return (
    <Screen
      id="pair"
      title="Pair"
      subtitle={credential?.account ?? undefined}
      banner={banner}
      actions={
        waiting ? (
          <Action label="Cancel" hint="B" kind="quiet" onPress={cancel} />
        ) : (
          <Action label="Start pairing" hint="A" kind="primary" onPress={start} busy={busy} />
        )
      }
    >
      <Stack gap="lg">
        {error ? (
          <ErrorState
            title="could not open the pairing window"
            detail={error}
            action={<Action label="Try again" onPress={start} />}
          />
        ) : null}

        {waiting ? <Waiting pairing={pairing} /> : <Idle pairing={pairing} onStart={start} busy={busy} />}

        {/* Three steps as three `Stat`s, which is the kit's only primitive for
            "a label and a line". Collapsed at 300 px, because after the first
            successful pair it is three lines of a five-line panel telling the
            player something they have already done. */}
        <Section
          title="what this does"
          description="Nothing is typed on the Deck, and the value never leaves your network."
          collapsible={{ compact: true, full: false }}
          defaultCollapsed={{ compact: true, full: false }}
        >
          <Stat size="sm" label="1 · here" value="Start pairing" note="the Deck shows an address and six digits" />
          <Stat
            size="sm"
            label="2 · on your PC"
            value="Open the address"
            note="type the six digits, paste the POESESSID cookie from pathofexile.com"
          />
          <Stat
            size="sm"
            label="3 · here again"
            value="Done"
            note="written to the Deck, and the page closes itself"
          />
        </Section>
      </Stack>
    </Screen>
  )
}

function Waiting({ pairing }: { pairing: PairingStatusPayload }): ReactElement {
  const url = pairing.urls[0]
  const attempts = pairing.attempts_left
  return (
    <Stack gap="lg">
      <Stat
        label="open this on your PC"
        value={url ?? 'no network address'}
        size={{ compact: 'md', full: 'lg' }}
        note={
          url
            ? pairing.urls.length > 1
              ? `also reachable at ${pairing.urls.slice(1).join(', ')}`
              : undefined
            : 'The Deck has no private network address, so nothing can reach it. Join wifi and try again.'
        }
        tone={url ? 'neutral' : 'warn'}
      />
      <Row gap="lg" align="end">
        <Stat
          label="code"
          value={pairing.code ?? '------'}
          size="lg"
          note={
            attempts !== null && attempts < 3
              ? `${attempts} attempt${attempts === 1 ? '' : 's'} left`
              : undefined
          }
          tone={attempts !== null && attempts < 3 ? 'warn' : 'neutral'}
        />
        {/* `Action`'s countdown prop runs the clock and disables itself at zero,
            which is exactly the shape of "this window closes on its own". The label
            is a statement rather than a control, so pressing it does nothing. */}
        <Action
          label="closes in"
          onPress={() => {}}
          countdown={pairing.expires_in}
          disabled
          kind="quiet"
        />
      </Row>
    </Stack>
  )
}

function Idle({
  pairing,
  onStart,
  busy,
}: {
  pairing: PairingStatusPayload
  onStart: () => void
  busy: boolean
}): ReactElement {
  const ending = ENDING_TEXT[pairing.state]
  if (pairing.state === 'paired') {
    // `detail` in preference to the canned line, because the backend's is more
    // specific: it names the account the session turned out to belong to, or says
    // the name is not known yet. That is the difference between "ready" and "ready,
    // and it will work out whose account this is on its next request" — neither a
    // failure, and one sentence cannot be both.
    return (
      <Empty
        title="Paired"
        detail={pairing.detail ?? ending}
        action={<Action label="Pair again" onPress={onStart} busy={busy} />}
      />
    )
  }
  return (
    <Empty
      title={ending ? 'Not paired' : 'Pair with your PC'}
      detail={
        ending ??
        'Press Start pairing. The Deck will show an address and six digits; open the address ' +
          'on your PC and paste the POESESSID cookie there.'
      }
      action={<Action label="Start pairing" kind="primary" onPress={onStart} busy={busy} />}
    />
  )
}

export default PairScreen
