/**
 * The Decky plugin entry point.
 *
 * `definePlugin` runs **once**, when Decky loads the plugin's frontend — not when
 * the QAM opens. That distinction is the whole reason this file is separate from
 * `Panel.tsx`: panel content is unmounted whenever the QAM closes (SPEC §6.2), so
 * the client, the transport and the event subscription are built here, above the
 * tree, and survive it. `content` is only the element to draw.
 *
 * `@decky/rollup` externalises `react` to `SP_REACT` and `@decky/ui` to `DFL`, so
 * this bundle shares Steam's React 19 instance rather than shipping one. The only
 * files that name `@decky/*` are this one, `bridge.ts`, and the kit's guard in
 * `@poedex/ui/steam`.
 */

import { definePlugin } from '@decky/api'
import { installClient } from '@poedex/core'
import { createDeckyClient } from './bridge'
import { Panel } from './Panel'

/**
 * The icon. Deliberately inline SVG rather than an asset: `@decky/rollup` would
 * rewrite an imported image to a `http://127.0.0.1:1337/plugins/...` URL, and a
 * plugin icon that depends on the loader's own web server being reachable is one
 * more thing to be wrong about.
 */
function PoedexIcon() {
  return (
    <svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" strokeWidth="1.8">
      {/* A bag with a coin in it, which is the whole product in two shapes. */}
      <path d="M4 8h16l-1.2 12H5.2L4 8Z" strokeLinejoin="round" />
      <path d="M9 8V6a3 3 0 0 1 6 0v2" strokeLinecap="round" />
      <circle cx="12" cy="14" r="2.5" />
    </svg>
  )
}

export default definePlugin(() => {
  // Once per plugin load, above the panel. A client built inside `Panel` would be
  // rebuilt every time the QAM opened, and every event between two openings would
  // land on a listener that no longer exists.
  installClient(createDeckyClient())

  return {
    name: 'PoEDex',
    titleView: <div style={{ display: 'flex', alignItems: 'center' }}>PoEDex</div>,
    content: <Panel />,
    icon: <PoedexIcon />,
    onDismount() {
      // Nothing to tear down: the transport's event listener is what should stay
      // attached, since the backend keeps pushing whether or not a panel is open,
      // and `installClient` is a module-level singleton by design (`runtime.ts`).
      // The backend's own shutdown is `_unload` in `plugin/main.py`.
    },
  }
})
