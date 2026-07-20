import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

import {
  boundaryZones,
  FETCH_SELECTORS,
  restrictedSyntax,
  TOKEN_SELECTORS,
} from "./eslint-rules.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    // TITAN-FRT-010 (token-only colors), TITAN-FRT-014 (no ad-hoc fetch), and
    // TITAN-FRT-049 (feature boundaries). Violating fixtures proving these rules
    // fail live in lint-fixtures/ (run via `make lint-proofs`).
    rules: {
      ...restrictedSyntax(TOKEN_SELECTORS, FETCH_SELECTORS),
      "import/no-restricted-paths": [
        "error",
        { basePath: `${__dirname}/src`, zones: boundaryZones(".") },
      ],
    },
  },
  {
    // TITAN-FRT-014 exemption: the BFF route handlers and the browser→BFF client
    // are the sanctioned places to call the network directly. Token rule stays.
    files: ["src/app/api/**/*.{ts,tsx}", "src/lib/bff/**/*.{ts,tsx}"],
    rules: {
      ...restrictedSyntax(TOKEN_SELECTORS),
    },
  },
  {
    // Tests mock fetch and drive internals — relax the fetch ban (token stays).
    // `e2e/**` is the Playwright suite (Phase 65): it mints its own session and
    // reads computed styles, so it needs the same latitude. Without this entry
    // it silently inherits the app's full rule set, ad-hoc-fetch ban included.
    files: [
      "tests/**/*.{ts,tsx}",
      "src/**/*.test.{ts,tsx}",
      "e2e/**/*.{ts,tsx}",
      "playwright.config.ts",
    ],
    rules: {
      ...restrictedSyntax(TOKEN_SELECTORS),
      // Playwright's fixture API is `async ({ page }, use) => { await use(x) }`.
      // The React plugin sees a bare `use(...)` call and reads it as React 19's
      // `use` hook in a non-component. It is not — this file has no React in it.
      "react-hooks/rules-of-hooks": "off",
    },
  },
  {
    ignores: [
      "node_modules/**",
      ".next/**",
      "out/**",
      "build/**",
      "next-env.d.ts",
      "lint-fixtures/**",
      ".storybook/**",
      "storybook-static/**",
      ".playwright/**",
      "playwright-report/**",
    ],
  },
];

export default eslintConfig;
