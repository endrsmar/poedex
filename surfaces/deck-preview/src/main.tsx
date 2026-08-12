/**
 * Pick a transport, install the client, mount the preview.
 *
 * The order is: ask whether a backend is answering, *then* mount. A preview that
 * started against fixtures and swapped to live data mid-session would be a third
 * behaviour to reason about, and the whole value of the mode chip in the header is
 * that it is true for the entire session.
 *
 * `baseUrl` is empty for the same reason `surfaces/web` leaves it empty: every request
 * is relative to this page's origin, and Vite proxies `/api` to `127.0.0.1:7331`.
 * There is no configurable backend address, because a configurable one is an address
 * that can be pointed at a machine that is not this one.
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { HttpTransport, createClient, installClient } from '@poedex/core'
import type { Transport } from '@poedex/core'
import { FixtureTransport, backendIsUp } from './fixtures'
import { Preview } from './Preview'
import type { DataMode } from './Preview'

async function start(): Promise<void> {
  const live = await backendIsUp()

  const transport: Transport = live ? new HttpTransport() : new FixtureTransport()
  const mode: DataMode = live ? 'backend' : 'fixtures'
  const note = live
    ? 'answering from poedex serve on 127.0.0.1:7331 — real account data, real rate-limit budget'
    : 'no backend on 127.0.0.1:7331 — answering from modules/*/ui/fixtures. Run `poedex serve` for real data'

  installClient(createClient(transport))
  transport.connect()

  const root = document.getElementById('root')
  if (!root) throw new Error('#root is missing from index.html')

  createRoot(root).render(
    <StrictMode>
      <Preview mode={mode} note={note} />
    </StrictMode>,
  )
}

void start()
