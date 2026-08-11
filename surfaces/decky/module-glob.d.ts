/**
 * Types for `module-glob.js`.
 *
 * The plugin itself is plain JavaScript because it is loaded by `rollup.config.js`
 * and by `vitest.config.ts`, neither of which compiles TypeScript before reading its
 * own config. This is the declaration that keeps `tsc --noEmit` over the workspace
 * honest about it.
 */

/** The virtual module id the plugin serves: `poedex:modules`. */
export declare const VIRTUAL_ID: string

/** Every `modules/<id>/ui/index.ts`, as `[repo-relative path, absolute path]`. */
export declare function findModuleUIs(root?: string): [string, string][]

/** A Rollup plugin (also accepted by Vite and vitest) serving {@link VIRTUAL_ID}. */
export default function poedexModules(root?: string): {
  name: string
  resolveId(id: string): string | null
  load(id: string): string | null
}
