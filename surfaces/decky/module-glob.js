/**
 * `import.meta.glob` for a Rollup build — one glob, used by both toolchains.
 *
 * IMPLEMENTATION-PLAN §3: *"the TS build globs `modules/<id>/ui`"*. The web shell
 * gets that from Vite. `@decky/rollup` is plain Rollup, which has no equivalent, and
 * the alternative — a hand-written list of module imports in the Decky shell — is
 * exactly the "list of features to keep in sync" the discovery mechanism exists to
 * avoid. A screen missing from that list would be missing from the Deck and present
 * on the web, which is the failure mode hardest to notice from a laptop.
 *
 * So the glob runs here, in the build, and emits a module that re-exports what it
 * found keyed by the same repo-relative path Vite's glob uses. It is a plain
 * Rollup plugin (`resolveId` + `load`), which Vite and vitest also accept, so
 * `vitest.config.ts` loads the *same* plugin and the tests see the same set the
 * bundle would.
 *
 * Deliberately `readdirSync` rather than a glob dependency: one directory level, one
 * filename, and a build-time dependency that scans the filesystem is not something
 * this project should be adding for that.
 */

import { readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const VIRTUAL_ID = 'poedex:modules'
const RESOLVED = '\0' + VIRTUAL_ID

/**
 * Repo root, from this file's location: `surfaces/decky/` -> `../..`.
 *
 * Resolved lazily and with a fallback because this module is loaded three ways: by
 * Rollup (a real `file:` URL), by vitest's config, and by a test that imports it
 * directly — and in that last case Vite can hand it an `import.meta.url` that
 * `fileURLToPath` refuses. `process.cwd()` is the repo root under both runners.
 */
function repoRoot() {
  try {
    return fileURLToPath(new URL('../../', import.meta.url))
  } catch {
    return process.cwd()
  }
}

/** Every `modules/<id>/ui/index.ts`, as `[repo-relative path, absolute path]`. */
export function findModuleUIs(root = repoRoot()) {
  const modulesDir = join(root, 'modules')
  const found = []
  let names
  try {
    names = readdirSync(modulesDir).sort()
  } catch {
    return found
  }
  for (const name of names) {
    const entry = join(modulesDir, name, 'ui', 'index.ts')
    try {
      if (statSync(entry).isFile()) found.push([`modules/${name}/ui/index.ts`, entry])
    } catch {
      // A module with a backend and no UI is the normal case, not a problem.
    }
  }
  return found
}

export default function poedexModules(root = repoRoot()) {
  return {
    name: 'poedex-modules',
    resolveId(id) {
      return id === VIRTUAL_ID ? RESOLVED : null
    },
    load(id) {
      if (id !== RESOLVED) return null
      const found = findModuleUIs(root)
      const imports = found
        .map(([, absolute], index) => `import * as m${index} from ${JSON.stringify(absolute)}`)
        .join('\n')
      const map = found
        .map(([relative], index) => `  ${JSON.stringify(relative)}: m${index},`)
        .join('\n')
      // `entries`, not a default export: `collectModuleUIs` takes the same shape
      // Vite's `import.meta.glob(..., { eager: true })` produces.
      return `${imports}\nexport const entries = {\n${map}\n}\n`
    },
  }
}
