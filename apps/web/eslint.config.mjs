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
    files: ["tests/**/*.{ts,tsx}", "src/**/*.test.{ts,tsx}"],
    rules: {
      ...restrictedSyntax(TOKEN_SELECTORS),
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
    ],
  },
];

export default eslintConfig;
