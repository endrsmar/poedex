/**
 * Module UI discovery.
 *
 * IMPLEMENTATION-PLAN §3: *"the TS build globs `modules/<id>/ui`"*. It does — literally,
 * here. The shell has no list of features to keep in sync; a module is present
 * because its directory is, which is the same rule the Python registry follows for
 * `modules/<id>/backend/module.py`.
 *
 * Discovery is deliberately dumb, and matches the Python side's caution: one
 * directory per module, one default export per module, nothing scanned for by
 * shape. A module whose `index.ts` does not default-export a `ModuleUI` is reported
 * rather than skipped, because a screen that silently fails to appear is the worst
 * possible failure of a plugin system.
 */

import type { ModuleUI, ProfileId } from '@poedex/ui'
import { screensFor } from '@poedex/ui'

const found = import.meta.glob<{ default?: ModuleUI }>('../../../modules/*/ui/index.ts', {
  eager: true,
})

export interface DiscoveredScreen {
  moduleId: string
  screenId: string
  title: string
  summary?: string
  component: React.ComponentType<{ moduleId: string; profile: ProfileId }>
  order: number
}

export interface Discovery {
  modules: ModuleUI[]
  screens: DiscoveredScreen[]
  problems: string[]
}

export function discoverModuleUIs(profile: ProfileId): Discovery {
  const modules: ModuleUI[] = []
  const screens: DiscoveredScreen[] = []
  const problems: string[] = []

  for (const [path, imported] of Object.entries(found).sort(([a], [b]) => a.localeCompare(b))) {
    const ui = imported?.default
    if (!ui || typeof ui.id !== 'string' || !Array.isArray(ui.screens)) {
      problems.push(`${path} does not default-export a module UI`)
      continue
    }
    modules.push(ui)
    for (const screen of screensFor(ui, profile)) {
      screens.push({
        moduleId: ui.id,
        screenId: screen.id,
        title: screen.title,
        summary: screen.summary,
        component: screen.component as DiscoveredScreen['component'],
        order: screen.order ?? 100,
      })
    }
    for (const [profileId, overridden] of Object.entries(ui.overrides ?? {})) {
      // §2.5: the framework logs which screens use overrides, so drift stays
      // visible instead of quietly becoming the norm.
      if (overridden?.length) {
        console.info(
          `poedex: ${ui.id} overrides ${overridden.join(', ')} for the ${profileId} profile`,
        )
      }
    }
  }

  screens.sort((a, b) => a.order - b.order || a.title.localeCompare(b.title))
  return { modules, screens, problems }
}
