// The Engineering Standards Constitution — Vol 6 §6, TypeScript surface.
// Installed by `titan-ci-init`. The layer graph here is the SAME graph the
// Python side enforces through import-linter (§6.1 TITAN-WFL-022): one
// constitution, two toolchains.
//
// Requires: eslint, eslint-plugin-boundaries, @typescript-eslint/parser.

import boundaries from 'eslint-plugin-boundaries';
import tsParser from '@typescript-eslint/parser';

// §6.1: domain <- application <- infrastructure <- interface.
// `allow` lists what a layer MAY import; everything else is a violation, so the
// rule is closed by default — a new layer cannot silently become reachable.
const LAYERS = [
  { type: 'domain', pattern: '*/domain/**' },
  { type: 'application', pattern: '*/application/**' },
  { type: 'infrastructure', pattern: '*/infrastructure/**' },
  { type: 'interface', pattern: '*/interface/**' },
];

export default [
  {
    files: ['**/*.ts', '**/*.tsx'],
    languageOptions: {
      parser: tsParser,
      parserOptions: { ecmaVersion: 'latest', sourceType: 'module' },
    },
    plugins: { boundaries },
    settings: {
      'boundaries/elements': LAYERS,
      'boundaries/include': ['**/*.ts', '**/*.tsx'],
    },
    rules: {
      // TITAN-WFL-022 — dependencies point inward only.
      'boundaries/element-types': [
        'error',
        {
          default: 'disallow',
          rules: [
            // The innermost layer depends on nothing. Zero framework imports.
            { from: 'domain', allow: [] },
            { from: 'application', allow: ['domain'] },
            { from: 'infrastructure', allow: ['domain', 'application'] },
            { from: 'interface', allow: ['domain', 'application', 'infrastructure'] },
          ],
        },
      ],
      'boundaries/no-unknown': 'error',

      // TITAN-WFL-027 — the §6.2 naming table, TypeScript rows.
      camelcase: ['error', { properties: 'never' }],

      // §6.4 TITAN-WFL-029 — structured logging, never console.
      'no-console': 'error',

      // §6.3 TITAN-WFL-028 / §6.6 R5 — no swallowed errors.
      'no-empty': ['error', { allowEmptyCatch: false }],
    },
  },
];
