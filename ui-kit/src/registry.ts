/**
 * Module UI registration (IMPLEMENTATION-PLAN §2.4).
 *
 * A module's `ui/index.ts` default-exports `defineModuleUI({...})`; a surface shell
 * discovers those and mounts them — the web shell as routes, the Decky shell as a
 * screen stack with **B** to go back. Neither shell knows what appraisal is.
 *
 * `profiles` is the interesting field. A screen declares where it makes sense, and
 * a dense stash browser saying `profiles: ['full']` is better than shipping a
 * cramped version of it at 300 px. The shell filters on it; nothing renders a
 * screen into a profile it disowned.
 */

import type { ComponentType } from 'react'
import type { ProfileId } from './profile'

export interface ModuleScreen<P = ModuleScreenProps> {
  id: string
  title: string
  /** One line for a nav item or a tab tooltip. */
  summary?: string
  component: ComponentType<P>
  profiles: ProfileId[]
  /** Lower sorts earlier in the shell's navigation. Defaults to 100. */
  order?: number
}

/**
 * What a shell hands every screen it mounts.
 *
 * Deliberately tiny. A screen gets its data from `@poedex/core` stores, which it
 * imports itself; the shell's job is to say which module and profile it is, not to
 * become a props pipeline that every module has to agree on.
 */
export interface ModuleScreenProps {
  moduleId: string
  profile: ProfileId
}

export interface ModuleUI {
  id: string
  screens: ModuleScreen<never>[]
  settings?: ComponentType<ModuleScreenProps>
  /**
   * Screens using `overrides/<profile>.tsx` (§2.5). Declared rather than detected,
   * so the framework can log which screens have drifted and drift stays visible
   * instead of quietly becoming the norm.
   */
  overrides?: Partial<Record<ProfileId, string[]>>
}

export interface ModuleUIInput {
  id: string
  /* Screens are heterogeneous by design; a shell only ever renders them with
     `ModuleScreenProps`, and requiring every screen to declare that exact prop
     type would make a screen that takes none a type error. */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  screens: ModuleScreen<any>[]
  settings?: ComponentType<ModuleScreenProps>
  overrides?: Partial<Record<ProfileId, string[]>>
}

export function defineModuleUI(input: ModuleUIInput): ModuleUI {
  const ids = new Set<string>()
  for (const screen of input.screens) {
    if (ids.has(screen.id)) {
      throw new Error(`${input.id}: two screens are both called '${screen.id}'`)
    }
    ids.add(screen.id)
    if (screen.profiles.length === 0) {
      throw new Error(
        `${input.id}.${screen.id}: declares no profiles, so nothing would ever mount it`,
      )
    }
  }
  return input as ModuleUI
}

/** Screens a given profile should mount, in navigation order. */
export function screensFor(ui: ModuleUI, profile: ProfileId): ModuleScreen<never>[] {
  return ui.screens
    .filter((screen) => screen.profiles.includes(profile))
    .sort((a, b) => (a.order ?? 100) - (b.order ?? 100) || a.title.localeCompare(b.title))
}
