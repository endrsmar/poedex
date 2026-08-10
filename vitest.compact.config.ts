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
 * Phase 7 replaces the `compact` implementations with `@decky/ui`-backed ones and
 * inherits this harness. If a contract has to change to make that work, the change
 * shows up here as a failing assertion rather than as a pile of overrides.
 */
export default mergeConfig(
  base,
  defineConfig({
    resolve: { conditions: ['poedex-compact'] },
    test: {
      name: 'compact',
      // The screen and kit tests are profile-agnostic by construction. The
      // transport, the store and the lint rule have no profile at all, so running
      // them twice would only slow the loop down.
      include: ['{ui-kit,modules}/**/*.test.{ts,tsx}'],
    },
  }),
)
