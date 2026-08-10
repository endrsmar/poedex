import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

const repoRoot = fileURLToPath(new URL('../..', import.meta.url))

/**
 * The `full` build.
 *
 * `resolve.conditions` is where the profile is chosen (IMPLEMENTATION-PLAN §2.3).
 * This build states nothing, so `#profile` falls through to `default` — the `full`
 * implementation. Phase 7's Rollup config adds `'poedex-compact'` and gets the
 * other one; neither bundle carries both.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The API lives on the Python transport. Proxying in dev means the dev server
    // and the built SPA use identical relative URLs, so there is no origin that
    // only exists while developing.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:7331',
        changeOrigin: false,
      },
    },
    fs: { allow: [repoRoot] },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // The Python transport serves `dist/assets` as a static mount and everything
    // else as the index. Keeping the default `assets/` name is what makes that
    // mount a two-line rule instead of a manifest reader.
    assetsDir: 'assets',
  },
})
