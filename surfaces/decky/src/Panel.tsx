/**
 * The Decky panel: a screen stack, **B** to go back, and nothing about appraisal.
 *
 * The web shell routes on the hash because a browser has one navigable axis. The QAM
 * has none — there is no address bar and no back gesture — so this is a stack:
 * `push` on picking a screen, `pop` on **B**, and the QAM's own close behaviour once
 * the stack is down to one. The shell does not import a screen; screens arrive from
 * `modules/<id>/ui/index.ts` (IMPLEMENTATION-PLAN §2.4), which is why adding a
 * feature is adding a directory.
 *
 * Three things the panel does that are specific to this surface:
 *
 * * **It opens on the screen that has a question in it.** With no usable session
 *   there is nothing a bag screen can say, so `credentials/pair` is the root rather
 *   than a tab somebody has to find. Once paired, the bag is the root and Pair is a
 *   place you can get back to.
 *
 * * **It names a broken `@decky/ui` rather than going white.** `@decky/ui` finds
 *   Steam's components by regex over minified code, so a client update can make one
 *   come back `undefined`. The kit's guard substitutes plain-DOM stand-ins and lists
 *   what was missing; this puts that list on screen, because a panel that quietly
 *   looks wrong is a bug report nobody can write.
 *
 * * **It shows a backend that failed to start.** `_main()` emits `backend.broken`
 *   rather than raising, since a raise leaves a plugin that appears in the list and
 *   does nothing, with the reason in a log file the player would have to leave
 *   gaming mode to read.
 *
 * The panel is unmounted whenever the QAM closes (SPEC §6.2), so nothing here holds
 * state worth keeping: the client, the transport and the event history live outside
 * the tree in `bridge.ts` and in the backend process.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import type { ReactElement } from 'react'
import { Focusable, steamComponentWarning } from '@poedex/ui/steam'
import { Action, ErrorState, PROFILE, Row, Screen, Section } from '@poedex/ui'
import { getClient } from '@poedex/core'
import type { CredentialStatusPayload } from '@poedex/core'
import { discoverModuleUIs } from './modules'

const PAIR = 'credentials/pair'

export function Panel(): ReactElement {
  const discovery = useMemo(() => discoverModuleUIs(PROFILE.id), [])
  const client = getClient()
  const [stack, setStack] = useState<string[]>([])
  const [credential, setCredential] = useState<CredentialStatusPayload | null>(null)
  const [broken, setBroken] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    const read = () => {
      void client.credentials
        .status()
        .then((status) => {
          if (live) setCredential(status)
        })
        .catch(() => {
          // The backend is still coming up. `backend.broken` is what says it never
          // will; a status call that failed once is not that.
        })
    }
    read()
    const off = [
      client.transport.on('credential_changed', read),
      client.transport.on('backend.broken', (event) =>
        setBroken(String(event.payload.detail ?? 'the backend did not start')),
      ),
      client.transport.on('backend.ready', () => {
        setBroken(null)
        read()
      }),
    ]
    return () => {
      live = false
      for (const stop of off) stop()
    }
  }, [client])

  // The root is a *derived* value, not stored state: the moment a credential
  // arrives, "where you are when the stack is empty" changes underneath you, and a
  // stored root would leave the player on the pairing screen after pairing.
  const root = credential && !credential.usable ? PAIR : (discovery.screens[0]
    ? `${discovery.screens[0].moduleId}/${discovery.screens[0].screenId}`
    : PAIR)
  const path = stack[stack.length - 1] ?? root
  const active = discovery.screens.find(
    (screen) => `${screen.moduleId}/${screen.screenId}` === path,
  )

  const back = useCallback(() => setStack((current) => current.slice(0, -1)), [])
  const open = useCallback(
    (next: string) => setStack((current) => (current[current.length - 1] === next ? current : [...current, next])),
    [],
  )

  const missing = steamComponentWarning()

  return (
    // **B is the back button.** `onCancel` is Steam's B, and returning without
    // popping lets the QAM close, which is what B means at the root. Intercepting it
    // unconditionally would trap the player in the panel.
    <Focusable
      flow-children="column"
      onCancel={(event: unknown) => {
        if (stack.length === 0) return
        ;(event as { stopPropagation?: () => void })?.stopPropagation?.()
        back()
      }}
    >
      {missing ? (
        <ErrorState title="Steam components missing" detail={missing} kind="unavailable" />
      ) : null}

      {broken ? (
        <ErrorState
          title="the backend did not start"
          detail={broken}
          action={
            <Action
              label="Reload"
              onPress={() => {
                void client.meta().then(() => setBroken(null))
              }}
            />
          }
        />
      ) : null}

      {discovery.problems.length > 0 ? (
        <ErrorState
          title="a module UI would not load"
          detail={discovery.problems.join(' · ')}
          kind="unavailable"
        />
      ) : null}

      {active ? (
        <active.component moduleId={active.moduleId} profile={PROFILE.id} />
      ) : (
        <Screen id="empty" title="PoEDex">
          <Section title="no screens">
            <ErrorState
              title="nothing to show"
              detail="No module UI declared a compact screen. This is a packaging fault, not a setting."
              kind="unavailable"
            />
          </Section>
        </Screen>
      )}

      {/* The nav is a footer row of `Action`s rather than a menu: at 300 px a menu
          costs a press to open and a press to close, and there are two entries. */}
      {discovery.screens.length > 1 ? (
        <Row gap="sm" wrap>
          {discovery.screens.map((screen) => {
            const id = `${screen.moduleId}/${screen.screenId}`
            return (
              <Action
                key={id}
                label={screen.title}
                kind={id === path ? 'primary' : 'quiet'}
                onPress={() => (id === root ? setStack([]) : open(id))}
              />
            )
          })}
          {stack.length > 0 ? <Action label="Back" hint="B" kind="quiet" onPress={back} /> : null}
        </Row>
      ) : null}
    </Focusable>
  )
}

export default Panel
