/**
 * The TypeScript half of IMPLEMENTATION-PLAN §2.6.
 *
 * The Python half is `tests/test_boundaries.py`, which walks the AST of every
 * module and refuses core→feature edges, undeclared dependencies and Decky imports.
 * This is the same idea for `modules/<id>/ui`:
 *
 * > may import `@poedex/ui`, its own module's types, and the frontend runtime;
 * > may **not** import `@decky/ui`, `react-dom`, CSS files, or another module's
 * > internals.
 *
 * Each rejection is a real failure mode rather than a tidiness preference:
 *
 * * **`@decky/ui`** — exists only inside the Decky plugin host. A module importing
 *   it works on hardware and nowhere else, which is precisely how a module stops
 *   being portable between the two surfaces. Reshaping for `compact` is the kit's
 *   job (§2.3).
 * * **`react-dom`** — a portal, a `createRoot`, or `flushSync` is raw DOM. There is
 *   no DOM to reach for under `compact`; there is Steam's tree.
 * * **CSS files** — `compact` is inline styles against `@decky/ui`. A module that
 *   ships a stylesheet has written a screen only one profile can render.
 * * **Another module's internals** — the Python rule is "only the dependency's
 *   `api.py`". The frontend equivalent is that a module's UI is not an API; if two
 *   features need to share a view, the shared thing belongs in the kit.
 * * **Escaping the module directory** — a relative path that climbs out of
 *   `modules/<id>/ui` is the same violation wearing a different hat.
 *
 * The rule is proved against violating source in `boundary.test.ts`. A boundary
 * checker only ever run over clean code is indistinguishable from one that always
 * returns nothing — the same argument `tests/test_boundaries.py` makes at the top
 * of its own docstring.
 */

const path = require('node:path')

const MODULE_UI = /(^|[\\/])modules[\\/]([^\\/]+)[\\/]ui[\\/]/

const ALLOWED_PACKAGES = new Set([
  '@poedex/ui',
  '@poedex/core',
  'react',
  'react/jsx-runtime',
  'react/jsx-dev-runtime',
])

const ALLOWED_PREFIXES = ['@poedex/ui/', '@poedex/core/']

const DENY = [
  {
    test: (source) => source === '@decky/ui' || source.startsWith('@decky/'),
    messageId: 'decky',
  },
  {
    test: (source) => source === 'react-dom' || source.startsWith('react-dom/'),
    messageId: 'reactDom',
  },
  {
    test: (source) => /\.(css|scss|sass|less|styl)(\?.*)?$/.test(source),
    messageId: 'styles',
  },
]

/** Which module a file belongs to, or null if it is not module UI at all. */
function moduleOf(filename) {
  const normalised = filename.replace(/\\/g, '/')
  const match = normalised.match(/(?:^|\/)modules\/([^/]+)\/ui\//)
  return match ? { id: match[1], root: normalised.slice(0, match.index) + `/modules/${match[1]}/ui` } : null
}

const boundaryRule = {
  meta: {
    type: 'problem',
    docs: {
      description:
        "what a module's UI may import: the kit, the frontend runtime, React, and its own files",
    },
    schema: [
      {
        type: 'object',
        properties: {
          // Extra package names this file may import. Used for a module's *test*
          // files, which legitimately need a runner and a rendering library. Every
          // deny rule still applies: a test may not import @decky/ui either, because
          // a test that only passes on hardware is not a test.
          allow: { type: 'array', items: { type: 'string' } },
        },
        additionalProperties: false,
      },
    ],
    messages: {
      decky:
        "{{source}}: a module's UI may not import a Decky API. Reshaping for the 300 px " +
        'panel is the kit\'s job — add a per-profile hint to a @poedex/ui primitive instead.',
      reactDom:
        '{{source}}: a module\'s UI may not import react-dom. There is no DOM to reach for ' +
        'under the compact profile.',
      styles:
        '{{source}}: a module\'s UI may not import a stylesheet. The compact profile is ' +
        'inline styles against @decky/ui; a screen with its own CSS renders on one surface.',
      otherModule:
        "{{source}}: a module's UI may not import another module's internals. If two " +
        'features need the same view, it belongs in @poedex/ui.',
      escapes:
        "{{source}}: this relative import leaves modules/{{module}}/ui. A module is a " +
        'vertical slice; anything it needs from outside comes through @poedex/ui or @poedex/core.',
      unknown:
        '{{source}}: not on the allowlist for module UI. Permitted: @poedex/ui, ' +
        '@poedex/core, react, and files inside this module.',
    },
  },

  create(context) {
    const filename = context.filename ?? context.getFilename()
    const owner = moduleOf(filename)
    if (!owner) return {}
    const extra = new Set((context.options[0] || {}).allow || [])

    function check(node, source) {
      if (typeof source !== 'string' || source === '') return

      for (const rule of DENY) {
        if (rule.test(source)) {
          context.report({ node, messageId: rule.messageId, data: { source } })
          return
        }
      }

      if (source.startsWith('.')) {
        const resolved = path
          .resolve(path.dirname(filename.replace(/\\/g, '/')), source)
          .replace(/\\/g, '/')
        if (!resolved.startsWith(owner.root)) {
          const messageId = MODULE_UI.test(`${resolved}/`) ? 'otherModule' : 'escapes'
          context.report({ node, messageId, data: { source, module: owner.id } })
        }
        return
      }

      if (/^@poedex\/(?!ui|core)/.test(source)) {
        context.report({ node, messageId: 'otherModule', data: { source } })
        return
      }

      if (ALLOWED_PACKAGES.has(source) || extra.has(source)) return
      if (ALLOWED_PREFIXES.some((prefix) => source.startsWith(prefix))) return
      if ([...extra].some((name) => source.startsWith(`${name}/`))) return

      context.report({ node, messageId: 'unknown', data: { source } })
    }

    return {
      ImportDeclaration(node) {
        check(node.source, node.source.value)
      },
      ExportNamedDeclaration(node) {
        if (node.source) check(node.source, node.source.value)
      },
      ExportAllDeclaration(node) {
        if (node.source) check(node.source, node.source.value)
      },
      ImportExpression(node) {
        if (node.source && node.source.type === 'Literal') check(node.source, node.source.value)
      },
    }
  },
}

module.exports = {
  rules: {
    'module-ui-boundary': boundaryRule,
  },
}
