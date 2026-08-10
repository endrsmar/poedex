/**
 * Proof that the boundary rule fails on a violation.
 *
 * `tests/test_boundaries.py` opens with the argument this file exists to satisfy:
 *
 * > A checker only ever run against clean code is indistinguishable from one that
 * > always returns [].
 *
 * So every forbidden import is exercised here as source the rule must reject, and
 * every permitted one as source it must accept. The repo's own `modules/<id>/ui`
 * passes `pnpm lint`, which is the other half — a rule that rejects everything is
 * just as useless as one that rejects nothing.
 */

import { RuleTester } from 'eslint'
import tsParser from '@typescript-eslint/parser'
import { describe, it } from 'vitest'
import plugin from './index.js'

// ESLint's RuleTester looks for these on the global object.
RuleTester.describe = describe as never
RuleTester.it = it as never
RuleTester.afterAll = (() => {}) as never

const tester = new RuleTester({
  languageOptions: {
    parser: tsParser as never,
    ecmaVersion: 2022,
    sourceType: 'module',
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
})

const IN_MODULE = '/repo/modules/appraisal/ui/BagScreen.tsx'
const NESTED = '/repo/modules/appraisal/ui/parts/Row.tsx'
const OUTSIDE = '/repo/surfaces/web/src/Shell.tsx'

tester.run('module-ui-boundary', plugin.rules['module-ui-boundary'], {
  valid: [
    // A module's *test* files get the runner and a rendering library through the
    // `allow` option — and nothing else. Every rejection below still applies to
    // them, which is the point: a test that imports @decky/ui is a test that only
    // passes on hardware.
    {
      code: "import { render } from '@testing-library/react'",
      filename: '/repo/modules/appraisal/ui/BagScreen.test.tsx',
      options: [{ allow: ['vitest', '@testing-library/react'] }],
    },
    { code: "import { Screen } from '@poedex/ui'", filename: IN_MODULE },
    { code: "import { createBagStore } from '@poedex/core'", filename: IN_MODULE },
    { code: "import type { ItemSet } from '@poedex/core/types'", filename: IN_MODULE },
    { code: "import { useState } from 'react'", filename: IN_MODULE },
    { code: "import { toRow } from './model'", filename: IN_MODULE },
    { code: "import fixture from './fixtures/bag-appraisal.json'", filename: IN_MODULE },
    { code: "import { toRow } from '../model'", filename: NESTED },
    { code: "export { BagScreen } from './BagScreen'", filename: IN_MODULE },

    // The shell is not module UI. It mounts trees and owns styling, so it is
    // allowed exactly the things a module is not.
    { code: "import { createRoot } from 'react-dom/client'", filename: OUTSIDE },
    { code: "import './shell.css'", filename: OUTSIDE },
  ],

  invalid: [
    {
      // ...including in a test file that was granted the runner.
      code: "import { Focusable } from '@decky/ui'",
      filename: '/repo/modules/appraisal/ui/BagScreen.test.tsx',
      options: [{ allow: ['vitest', '@testing-library/react'] }],
      errors: [{ messageId: 'decky' }],
    },
    {
      // The violation this rule exists for. Under `compact` a module that reaches
      // for @decky/ui works on hardware and nowhere else.
      code: "import { Focusable } from '@decky/ui'",
      filename: IN_MODULE,
      errors: [{ messageId: 'decky' }],
    },
    {
      code: "import { callable } from '@decky/api'",
      filename: IN_MODULE,
      errors: [{ messageId: 'decky' }],
    },
    {
      code: "import { createPortal } from 'react-dom'",
      filename: IN_MODULE,
      errors: [{ messageId: 'reactDom' }],
    },
    {
      code: "import './bag.css'",
      filename: IN_MODULE,
      errors: [{ messageId: 'styles' }],
    },
    {
      code: "import { thing } from '../../prices/ui/PriceTable'",
      filename: IN_MODULE,
      errors: [{ messageId: 'otherModule' }],
    },
    {
      code: "import { PriceTable } from '@poedex/prices-ui'",
      filename: IN_MODULE,
      errors: [{ messageId: 'otherModule' }],
    },
    {
      code: "import { Shell } from '../../../surfaces/web/src/Shell'",
      filename: IN_MODULE,
      errors: [{ messageId: 'escapes' }],
    },
    {
      code: "import axios from 'axios'",
      filename: IN_MODULE,
      errors: [{ messageId: 'unknown' }],
    },
    {
      // A dynamic import is still an import.
      code: "const mod = await import('@decky/ui')",
      filename: IN_MODULE,
      errors: [{ messageId: 'decky' }],
    },
    {
      code: "export * from '@decky/ui'",
      filename: IN_MODULE,
      errors: [{ messageId: 'decky' }],
    },
  ],
})
