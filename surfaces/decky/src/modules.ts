/**
 * Module UI discovery, the `compact` half.
 *
 * Same rule as the web shell — *a module is present because its directory is* — and
 * the same `collectModuleUIs` from `@poedex/ui` after the glob. Only the glob is
 * different, and it has to be: `import.meta.glob` is Vite's, and this bundle is
 * built by Rollup through `@decky/rollup`. `module-glob.js` therefore resolves the
 * virtual module `poedex:modules` by walking `modules/<id>/ui/index.ts` and emitting
 * one `import` per hit.
 *
 * Two shells finding modules two ways is a real risk — a screen that mounts on one
 * surface and not the other, with nobody noticing — which is why the *only*
 * difference is where the map of entries comes from and why
 * `surfaces/decky/src/modules.test.ts` asserts the two shells agree.
 */

import type { Discovery, ProfileId } from '@poedex/ui'
import { collectModuleUIs } from '@poedex/ui'
import { entries } from 'poedex:modules'

export type { Discovery, DiscoveredScreen } from '@poedex/ui'

export function discoverModuleUIs(profile: ProfileId): Discovery {
  return collectModuleUIs(entries, profile)
}
