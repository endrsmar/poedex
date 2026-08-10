/**
 * Module UI discovery.
 *
 * The shell has no list of features. This asserts the glob actually finds the one
 * module that ships a UI, that the screen it declares is the one that gets mounted,
 * and that a screen declaring only `full` is not offered to `compact` — which is the
 * mechanism that lets a future stash browser say "not at 300 px" out loud.
 */

import { describe, expect, it } from 'vitest'
import { defineModuleUI, screensFor } from '@poedex/ui'
import { discoverModuleUIs } from './modules'

describe('discoverModuleUIs', () => {
  it('finds the appraisal module without the shell naming it', () => {
    const found = discoverModuleUIs('full')
    expect(found.problems).toEqual([])
    expect(found.modules.map((module) => module.id)).toContain('appraisal')
    expect(found.screens.map((screen) => `${screen.moduleId}/${screen.screenId}`)).toContain(
      'appraisal/bag',
    )
  })

  it('offers the bag screen at both densities', () => {
    expect(discoverModuleUIs('compact').screens.map((s) => s.screenId)).toContain('bag')
  })
})

describe('defineModuleUI', () => {
  it('refuses a screen that declares no profile, because nothing would mount it', () => {
    expect(() =>
      defineModuleUI({ id: 'x', screens: [{ id: 'a', title: 'A', component: () => null, profiles: [] }] }),
    ).toThrow(/no profiles/)
  })

  it('refuses two screens with the same id', () => {
    const screen = { id: 'a', title: 'A', component: () => null, profiles: ['full' as const] }
    expect(() => defineModuleUI({ id: 'x', screens: [screen, { ...screen }] })).toThrow(/both called/)
  })

  it('filters by profile, so a screen can decline a density', () => {
    const ui = defineModuleUI({
      id: 'x',
      screens: [
        { id: 'bag', title: 'Bag', component: () => null, profiles: ['compact', 'full'] },
        { id: 'stash', title: 'Stash', component: () => null, profiles: ['full'] },
      ],
    })
    expect(screensFor(ui, 'compact').map((s) => s.id)).toEqual(['bag'])
    expect(screensFor(ui, 'full').map((s) => s.id)).toEqual(['bag', 'stash'])
  })
})
