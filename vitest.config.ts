import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

/**
 * One config for the whole workspace.
 *
 * `resolve.conditions` is where the surface profile is chosen: the default run
 * resolves `#profile` to `full`. Phase 7 adds a second project with
 * `conditions: ['poedex-compact', ...]` and the same component tests run against
 * the `compact` implementations without a line of test code changing — which is
 * the only way to find out whether the contracts really survived 300 px.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['{ui-kit,frontend,surfaces,modules,tools}/**/*.test.{ts,tsx}'],
    exclude: ['**/node_modules/**', '**/dist/**'],
    restoreMocks: true,
  },
})
