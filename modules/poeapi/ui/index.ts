/**
 * The `poeapi` module's own UI (IMPLEMENTATION-PLAN §1.2, §2.4).
 *
 * The second **core** module with a face, and it earns one the same way
 * `credentials` does: which character to read is `poeapi`'s question, the answer
 * lives in `poeapi`'s settings, and "core" limits what a module may depend on and
 * decide — not whether it has a screen.
 */

import { defineModuleUI } from '@poedex/ui'
import { CharacterScreen } from './CharacterScreen'

export default defineModuleUI({
  id: 'poeapi',
  screens: [
    {
      id: 'character',
      title: 'Character',
      summary: 'Which character the tool reads, and how to read a different one.',
      component: CharacterScreen,
      // Both, and `compact` is the one that matters. On a Deck this screen is the
      // *only* way to change the character at all: the plugin's settings live under
      // `DECKY_PLUGIN_SETTINGS_DIR` rather than `~/.config/poedex`, and there is no
      // usable CLI inside the plugin tree to run `poedex config set` with.
      profiles: ['compact', 'full'],
      // Between the bag (10) and the stash (20). It is the screen you visit when
      // the bag shows something you did not expect, which is the moment it has to
      // be one press away rather than at the end of the list.
      order: 15,
    },
  ],
})

export { CharacterScreen } from './CharacterScreen'
export * from './model'
