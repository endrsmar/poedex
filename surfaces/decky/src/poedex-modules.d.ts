/**
 * The virtual module `rollup.config.js` generates.
 *
 * Rollup has no `import.meta.glob`, so `module-glob.js` walks `modules/<id>/ui/` in
 * the build and emits a module re-exporting what it found, keyed by the same
 * repo-relative path the Vite glob uses. Declaring it here is what lets `tsc` see it;
 * `vitest.config.ts` loads the *same* plugin, so a test cannot see a different set of
 * module UIs from the bundle.
 */
declare module 'poedex:modules' {
  import type { ModuleUI } from '@poedex/ui'

  export const entries: Record<string, { default?: ModuleUI } | undefined>
}
