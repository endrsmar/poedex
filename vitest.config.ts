import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import poedexModules from './surfaces/decky/module-glob.js'

/**
 * `@decky/ui` cannot be imported outside Steam, so it is aliased in both configs.
 *
 * Its `webpack.js` walks `window.webpackChunksteamui` at module scope and
 * `components/Dialog.js` then evaluates `Object.values(CommonUIModule)` on the
 * `undefined` that comes back — the import throws before a single test can run.
 * Measured against `@decky/ui@4.12.0`, not assumed.
 *
 * `ui-kit/src/profiles/compact/steam.tsx` is the only file that imports the package,
 * and it is written to work whether a component arrives or not — which is the same
 * guard the real hardware needs, since `@decky/ui` finds Steam's components by regex
 * over minified code. `ui-kit/src/testing/decky-ui-double.tsx` states plainly what
 * running against stand-ins does and does not prove.
 */
export const DECKY_UI_DOUBLE = fileURLToPath(
  new URL('./ui-kit/src/testing/decky-ui-double.tsx', import.meta.url),
)

/**
 * One config for the whole workspace.
 *
 * `resolve.conditions` is where the surface profile is chosen: the default run
 * resolves `#profile` to `full`. `vitest.compact.config.ts` sets
 * `conditions: ['poedex-compact']` and the same component tests run against the
 * `compact` implementations without a line of test code changing — which is
 * the only way to find out whether the contracts really survived 300 px.
 */
export default defineConfig({
  // `poedexModules` supplies `poedex:modules`, the Decky shell's stand-in for
  // `import.meta.glob`. The *same* plugin the Rollup build uses, so a test cannot
  // see a different set of module UIs from the bundle.
  plugins: [react(), poedexModules()],
  resolve: { alias: { '@decky/ui': DECKY_UI_DOUBLE } },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    // `surfaces/web` rather than `surfaces`, and the omission is deliberate: the
    // Decky shell is a `compact`-only surface whose tests assert `compact`-only
    // facts — that the stash screen is not offered at 300 px, that the pairing
    // screen is the root without a session. Running them with `#profile` resolved to
    // `full` would assert the opposite. `vitest.compact.config.ts` adds them to its
    // own `include`, which is the run that has the right profile.
    include: [
      '{ui-kit,frontend,modules,tools}/**/*.test.{ts,tsx}',
      'surfaces/web/**/*.test.{ts,tsx}',
    ],
    exclude: ['**/node_modules/**', '**/dist/**'],
    restoreMocks: true,
  },
})
