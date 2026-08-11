import { defineConfig, mergeConfig } from 'vitest/config'
import base from './vitest.config'

/**
 * The same tests, against the `compact` profile.
 *
 * This is Phase 5's answer to its own stated risk. Designing primitives while
 * implementing one profile can produce contracts that do not fit the other, and the
 * only way to find out is to run the same assertions through the other
 * implementation. `resolve.conditions` flips `#profile` to the `compact` build; not
 * one line of test code changes, because no test names a profile.
 *
 * It is a separate config rather than a second `include` because the profile is a
 * *build-time* choice — the whole point of §2.3 is that neither bundle carries the
 * other — so one process cannot hold both.
 *
 *     pnpm run test           # full
 *     pnpm run test:compact   # compact
 *
 * **Phase 7 swapped the `compact` elements for `@decky/ui` components and this
 * harness held.** Not one contract changed, not one hint moved, and no module gained
 * an `overrides/compact.tsx` — which is the claim §2.5 exists to keep falsifiable.
 * `@decky/ui` itself is aliased to stand-ins (see the base config); what this run
 * proves is that the compact primitives delegate to it with the right props and that
 * every assertion about content still holds. What it cannot prove is anything about
 * real `@decky/ui` on real hardware — `docs/deck-checklist.md`.
 */
export default mergeConfig(
  base,
  defineConfig({
    resolve: { conditions: ['poedex-compact'] },
    test: {
      name: 'compact',
      // `mergeConfig` *concatenates* `include`, so this adds to the base list rather
      // than replacing it: the compact run is everything the default run does, plus
      // the Decky shell, with `#profile` flipped. `surfaces/decky` appears here and
      // only here, because it is the compact surface.
      include: ['surfaces/decky/**/*.test.{ts,tsx}'],
    },
  }),
)
