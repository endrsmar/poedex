/**
 * Module discovery, the Decky half.
 *
 * The shell has no list of features; a module is present because its directory is.
 * Two shells finding modules two different ways — Vite's `import.meta.glob` and the
 * Rollup plugin in `module-glob.js` — is a real risk, and it is the kind that is
 * invisible from a laptop: a screen mounted on the web and missing from the Deck.
 * So the glob is asserted directly rather than through what it produces.
 */

import { describe, expect, it } from 'vitest'
import { findModuleUIs } from '../module-glob.js'
import { discoverModuleUIs } from './modules'

describe('the Rollup glob', () => {
  it('finds every modules/<id>/ui/index.ts, and nothing else', () => {
    const found = findModuleUIs().map(([relative]) => relative)
    expect(found).toContain('modules/appraisal/ui/index.ts')
    expect(found).toContain('modules/credentials/ui/index.ts')
    // A *core* module with a face, like `credentials`. On a Deck the picker is the
    // only way to change which character is read at all.
    expect(found).toContain('modules/poeapi/ui/index.ts')
    // A module with a backend and no UI is the normal case, not a problem.
    expect(found).not.toContain('modules/moddb/ui/index.ts')
    expect(found).toEqual([...found].sort())
  })

  it('keys entries by the same repo-relative path Vite uses', () => {
    // `surfaces/web/src/modules.ts` globs `../../../modules/*/ui/index.ts`, so its
    // keys are relative to that file. The *shape* is what has to match, because both
    // feed `collectModuleUIs`, which only ever puts the key in a problem message.
    for (const [relative] of findModuleUIs()) {
      expect(relative).toMatch(/^modules\/[^/]+\/ui\/index\.ts$/)
    }
  })
})

describe('discoverModuleUIs at compact', () => {
  it('mounts the bag, the character picker and the pairing screen', () => {
    const found = discoverModuleUIs('compact')
    expect(found.problems).toEqual([])
    expect(found.screens.map((screen) => `${screen.moduleId}/${screen.screenId}`)).toEqual([
      'appraisal/bag',
      // Second, deliberately. It is the screen you go to when the bag shows
      // something you did not expect, which is the moment it has to be one press
      // away rather than at the end of the list.
      'poeapi/character',
      'credentials/pair',
    ])
  })

  it('does not mount the stash browser, because it declared full only', () => {
    // Not an omission to fix. `StashScreen` says `profiles: ['full']` and argues the
    // case in its own docstring; the compact answer is a digest, and that is a
    // different screen rather than this one squeezed.
    expect(discoverModuleUIs('compact').screens.map((s) => s.screenId)).not.toContain('stash')
    expect(discoverModuleUIs('full').screens.map((s) => s.screenId)).toContain('stash')
  })

  it('sorts by declared order, so the panel opens on the bag', () => {
    expect(discoverModuleUIs('compact').screens[0]?.screenId).toBe('bag')
  })
})
