/**
 * Module UI discovery, the `full` half.
 *
 * IMPLEMENTATION-PLAN §3: *"the TS build globs `modules/<id>/ui`"*. It does —
 * literally, here. The shell has no list of features to keep in sync; a module is
 * present because its directory is, which is the same rule the Python registry
 * follows for `modules/<id>/backend/module.py`.
 *
 * Everything after the glob is `collectModuleUIs` in `@poedex/ui`, shared with the
 * Decky shell. Only the glob differs, because `import.meta.glob` is Vite's and the
 * Decky bundle is Rollup's — see `surfaces/decky/rollup.config.js`.
 */

import type { Discovery, ModuleUI, ProfileId } from '@poedex/ui'
import { collectModuleUIs } from '@poedex/ui'

export type { Discovery, DiscoveredScreen } from '@poedex/ui'

const found = import.meta.glob<{ default?: ModuleUI }>('../../../modules/*/ui/index.ts', {
  eager: true,
})

export function discoverModuleUIs(profile: ProfileId): Discovery {
  return collectModuleUIs(found, profile)
}
