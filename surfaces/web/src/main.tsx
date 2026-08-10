import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { HttpTransport, createClient } from '@poedex/core'
import { Shell } from './Shell'

/**
 * `baseUrl` is empty on purpose: every request is relative to the page's own
 * origin, which is `http://127.0.0.1:7331` when the Python transport serves the
 * build and the Vite dev server's proxy when developing. There is no configurable
 * backend address, because a configurable one is an address that can be pointed
 * somewhere that is not this machine.
 */
const transport = new HttpTransport()
const client = createClient(transport)
transport.connect()

const root = document.getElementById('root')
if (!root) throw new Error('#root is missing from index.html')

createRoot(root).render(
  <StrictMode>
    <Shell client={client} />
  </StrictMode>,
)
