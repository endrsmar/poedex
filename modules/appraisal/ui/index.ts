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
import { StashScreen } from './StashScreen'

export default defineModuleUI({
  id: 'appraisal',
  screens: [
    {
      id: 'bag',
      title: 'Bag',
      summary: 'Is any of this worth a stash trip?',
      component: BagScreen,
      // The bag screen is the reason the project exists and works at both
      // densities. The stash browser below declares `['full']`, for the reason
      // §2.4 anticipated: a dense browser genuinely does not belong at 300 px.
      profiles: ['compact', 'full'],
      order: 10,
    },
    {
      id: 'stash',
      title: 'Stash',
      summary: 'Which tabs are worth opening, and what is in the one you opened.',
      component: StashScreen,
      // **`full` only, and that is the decision §2.4 exists for.** A 117-row tab
      // list and a 24x24 quad tab do not fit at 300 px, and research-notes §8 found
      // that the bag grid's justification does not transfer to the stash: when you
      // are looking at your stash you are standing at it, and the game shows it
      // full size. The compact answer is a *digest* — a ranked list of tabs worth
      // walking to — and that is a different screen, not this one squeezed.
      // Saying so is better than shipping a cramped version of it.
      profiles: ['full'],
      order: 20,
    },
  ],
})

export { BagScreen } from './BagScreen'
export { StashScreen } from './StashScreen'
export { PriceCheck } from './PriceCheck'
export * from './model'
