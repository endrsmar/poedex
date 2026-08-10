/**
 * The `appraisal` module's UI registration (IMPLEMENTATION-PLAN §2.4).
 *
 * A module is a vertical slice: this directory sits beside `backend/`, and nothing
 * about appraisal lives in a surface. A shell discovers this default export and
 * mounts what it declares — the web shell as a route, the Decky shell as a screen
 * in its stack.
 */

import { defineModuleUI } from '@poedex/ui'
import { BagScreen } from './BagScreen'

export default defineModuleUI({
  id: 'appraisal',
  screens: [
    {
      id: 'bag',
      title: 'Bag',
      summary: 'Is any of this worth a stash trip?',
      component: BagScreen,
      // The bag screen is the reason the project exists and works at both
      // densities. The stash digest, when it lands, will declare `['full']` — a
      // dense browser genuinely does not belong at 300 px, and saying so is better
      // than shipping a cramped version of it (§2.4).
      profiles: ['compact', 'full'],
      order: 10,
    },
  ],
})

export { BagScreen } from './BagScreen'
export * from './model'
