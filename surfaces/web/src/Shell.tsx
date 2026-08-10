/**
 * The `full` surface shell.
 *
 * Its whole job is: install a client, discover module UIs, and mount the one the
 * URL asks for. It knows nothing about appraisal — there is no import of a screen
 * in this file, and adding a second feature module means adding a directory, not
 * editing the shell.
 *
 * Routing is the hash, not a router library. There is one navigable axis
 * (`#/<module>/<screen>`), the Decky shell will have none at all (a screen stack
 * with **B** to go back), and a router would be the shell's opinion imposed on a
 * surface that cannot use it.
 */

import { useEffect, useMemo, useState, useSyncExternalStore } from 'react'
import type { ReactElement } from 'react'
import { installClient } from '@poedex/core'
import type { PoedexClient, TransportState } from '@poedex/core'
import { FULL_PROFILE } from '@poedex/ui'
import { discoverModuleUIs } from './modules'
import './shell.css'

export interface ShellProps {
  client: PoedexClient
}

export function Shell({ client }: ShellProps): ReactElement {
  // Before the first screen mounts and once per client, not once per render: a
  // module screen calls `getClient()` on its way up.
  useMemo(() => installClient(client), [client])
  const discovery = useMemo(() => discoverModuleUIs(FULL_PROFILE.id), [])
  const [route, setRoute] = useState(() => readRoute())
  const connection = useTransportState(client)

  useEffect(() => {
    const onHashChange = () => setRoute(readRoute())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const active =
    discovery.screens.find(
      (screen) => screen.moduleId === route.moduleId && screen.screenId === route.screenId,
    ) ?? discovery.screens[0]

  return (
    <div className="pk-root shell">
      <nav className="shell__nav">
        <span className="shell__brand">PoEDex</span>
        {discovery.screens.map((screen) => (
          <button
            key={`${screen.moduleId}/${screen.screenId}`}
            type="button"
            className="shell__tab"
            aria-current={screen === active ? 'page' : undefined}
            title={screen.summary}
            onClick={() => {
              window.location.hash = `#/${screen.moduleId}/${screen.screenId}`
            }}
          >
            {screen.title}
          </button>
        ))}
        <span className="shell__status" data-state={connection}>
          <span aria-hidden="true">●</span>
          {connection === 'open' ? 'live' : connection}
        </span>
      </nav>

      {discovery.problems.length > 0 ? (
        <div className="shell__problems" role="alert">
          {discovery.problems.join(' · ')}
        </div>
      ) : null}

      <div className="shell__screen">
        {active ? (
          <active.component moduleId={active.moduleId} profile={FULL_PROFILE.id} />
        ) : (
          <p style={{ padding: '2rem' }}>
            No module UI was found under <code>modules/*/ui</code>.
          </p>
        )}
      </div>
    </div>
  )
}

function useTransportState(client: PoedexClient): TransportState {
  return useSyncExternalStore(
    (listener) => client.transport.onStateChange(() => listener()),
    () => client.transport.state,
    () => 'idle' as TransportState,
  )
}

function readRoute(): { moduleId: string; screenId: string } {
  const [, moduleId = '', screenId = ''] = window.location.hash.replace(/^#\/?/, '/').split('/')
  return { moduleId, screenId }
}
