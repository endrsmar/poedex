import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import poedexModules from '../decky/module-glob.js'

const repoRoot = fileURLToPath(new URL('../..', import.meta.url))

/**
 * A `compact` build that runs in a browser.
 *
 * Three lines do the whole job, and each one is copied from somewhere that already
 * had to solve it rather than invented here:
 *
 * * **`conditions: ['poedex-compact', …]`** — the profile switch
 *   (IMPLEMENTATION-PLAN §2.3), the same one `vitest.compact.config.ts` and
 *   `surfaces/decky/rollup.config.js` set. The defaults are re-listed after it because
 *   `resolve.conditions` replaces Vite's list rather than adding to it, and dropping
 *   `browser`/`module` breaks every dependency that publishes both builds.
 * * **`alias: { '@decky/ui': … }`** — the package cannot be imported outside Steam
 *   (`vitest.config.ts` has the traceback), so this points at the same stand-ins the
 *   test suite uses. It is why the preview is a *simulation* of the QAM and not the
 *   QAM, and the shell says so on screen.
 * * **`poedexModules()`** — `poedex:modules`, the Decky shell's stand-in for
 *   `import.meta.glob`. The *same* plugin the Rollup build and vitest load, so the
 *   preview cannot show a different set of module screens from the plugin.
 *
 * There is no `build` target on purpose. This is a dev tool: `pnpm run deck` starts it
 * and nothing produces an artifact from it, which is also the simplest way to keep it
 * out of `scripts/build_plugin.py`'s reach.
 */
export default defineConfig({
  plugins: [react(), poedexModules(repoRoot)],
  resolve: {
    conditions: ['poedex-compact', 'module', 'browser', 'development|production'],
    alias: {
      '@decky/ui': fileURLToPath(
        new URL('../../ui-kit/src/testing/decky-ui-double.tsx', import.meta.url),
      ),
    },
  },
  server: {
    port: 5174,
    // Same proxy as `surfaces/web`, so both surfaces use identical relative URLs and
    // there is no origin that exists only while developing.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:7331',
        changeOrigin: false,
      },
    },
    fs: { allow: [repoRoot] },
  },
})
