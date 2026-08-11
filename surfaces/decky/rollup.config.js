import { fileURLToPath } from 'node:url'
import { nodeResolve } from '@rollup/plugin-node-resolve'
import deckyPlugin from '@decky/rollup'
import poedexModules from './module-glob.js'

/**
 * The `compact` bundle.
 *
 * `@decky/rollup` supplies the parts that are the same for every Decky plugin:
 * `react` -> `SP_REACT`, `react/jsx-runtime` -> `SP_JSX`, `@decky/ui` -> `DFL`, all
 * three external, ESM into `dist/`. It reads its manifest from `plugin/plugin.json`,
 * which is the one this repo ships — the plugin name has to match between the
 * manifest and the bundle's asset URLs, so there is exactly one of them.
 *
 * Two plugins are added in front of the preset's, and order matters: Rollup asks
 * plugins to resolve in order, so these get the chance first.
 *
 * 1. **`nodeResolve` with `exportConditions: ['poedex-compact']`.** This is how the
 *    profile is chosen. `@poedex/ui` publishes `#profile` as a package-imports
 *    subpath resolving to `profiles/compact` under that condition and to
 *    `profiles/full` otherwise (IMPLEMENTATION-PLAN §2.3), so neither bundle carries
 *    the other's implementation. Getting it wrong is not subtle — the `full` profile
 *    imports a stylesheet and the build stops — which is the right kind of failure
 *    for a build-time switch.
 *
 * 2. **The module glob.** Rollup has no `import.meta.glob`; `module-glob.js` is the
 *    replacement, and `vitest.config.ts` loads the same plugin so the tests see the
 *    same set the bundle would.
 *
 * `input` is not overridden and neither is `preserveEntrySignatures`:
 * `mergeAndConcat` lets the preset's scalars win, and the preset pairs
 * `output.exports: 'default'` with the entry's default export — the descriptor Decky
 * calls. Run this from `surfaces/decky`.
 */
export default deckyPlugin(
  {
    plugins: [
      nodeResolve({
        browser: true,
        exportConditions: ['poedex-compact'],
        extensions: ['.mjs', '.js', '.json', '.node', '.ts', '.tsx'],
      }),
      poedexModules(),
    ],
  },
  fileURLToPath(new URL('../../plugin/', import.meta.url)),
)
