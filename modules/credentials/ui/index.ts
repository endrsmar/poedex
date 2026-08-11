/**
 * The `credentials` module's own UI (IMPLEMENTATION-PLAN §1.2, §2.4).
 *
 * A **core** module with a face, which the plan anticipated in as many words:
 * *"`credentials` needs a pairing screen; 'core' limits what a module may depend on
 * and decide, not whether it has one."* Nothing about the credential lives in a
 * surface — the Decky shell mounts this screen because the module registered it, the
 * same way the web shell mounts the bag.
 */

import { defineModuleUI } from '@poedex/ui'
import { PairScreen } from './PairScreen'

export default defineModuleUI({
  id: 'credentials',
  screens: [
    {
      id: 'pair',
      title: 'Pair',
      summary: 'Put a POESESSID on the Deck without typing one.',
      component: PairScreen,
      // Both. Pairing is not a small-screen workaround — a desktop user who would
      // rather not open a terminal for `poedex auth set` wants the same page — and
      // declaring both is what puts this screen through the two-profile harness.
      profiles: ['compact', 'full'],
      // Last in the nav. It is the screen you visit once and then never again, and
      // the shell opens it by itself when there is no usable session.
      order: 90,
    },
  ],
})

export { PairScreen } from './PairScreen'
