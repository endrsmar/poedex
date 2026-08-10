import js from '@eslint/js'
import globals from 'globals'
import tseslint from 'typescript-eslint'
import poedex from 'eslint-plugin-poedex'

export default tseslint.config(
  {
    ignores: [
      '**/node_modules/**',
      '**/dist/**',
      '.venv/**',
      // Generated from the pydantic models. Fix the generator, not the output.
      'frontend/core/src/types/generated.ts',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    rules: {
      '@typescript-eslint/consistent-type-imports': [
        'error',
        { prefer: 'type-imports', fixStyle: 'separate-type-imports' },
      ],
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  },

  /**
   * IMPLEMENTATION-PLAN §2.6, the TypeScript half. Scoped to module UI only: the
   * kit may import CSS (it is the profile that owns styling) and the shell may
   * import react-dom (it is the thing that mounts a tree).
   */
  {
    files: ['modules/*/ui/**/*.{ts,tsx}'],
    plugins: { poedex },
    rules: {
      'poedex/module-ui-boundary': 'error',
    },
  },

  /**
   * A module's own tests are still module UI and still may not import `@decky/ui`,
   * `react-dom`, a stylesheet or another module. They do need a runner and a way to
   * render, so those three names — and only those — are granted.
   */
  {
    files: ['modules/*/ui/**/*.test.{ts,tsx}'],
    rules: {
      'poedex/module-ui-boundary': [
        'error',
        { allow: ['vitest', '@testing-library/react', '@testing-library/user-event'] },
      ],
    },
  },

  {
    files: ['**/*.test.{ts,tsx}'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },

  {
    files: ['tools/eslint-plugin-poedex/index.js'],
    languageOptions: { sourceType: 'commonjs', globals: globals.node },
    rules: { '@typescript-eslint/no-require-imports': 'off' },
  },
)
