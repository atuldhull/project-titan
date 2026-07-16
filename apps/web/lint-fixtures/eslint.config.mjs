// Fixture config: applies the SAME rule objects as the app config, rooted at
// the fixture tree, so `make lint-proofs` proves the production rules fire.
import importPlugin from "eslint-plugin-import";
import tsParser from "@typescript-eslint/parser";
import { dirname } from "path";
import { fileURLToPath } from "url";

import {
  boundaryZones,
  FETCH_SELECTORS,
  restrictedSyntax,
  TOKEN_SELECTORS,
} from "../eslint-rules.mjs";

const base = dirname(fileURLToPath(import.meta.url));

export default [
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: { parser: tsParser },
    plugins: { import: importPlugin },
    settings: {
      "import/resolver": { node: { extensions: [".ts", ".tsx", ".js"] } },
    },
    rules: {
      ...restrictedSyntax(TOKEN_SELECTORS, FETCH_SELECTORS),
      "import/no-restricted-paths": ["error", { basePath: base, zones: boundaryZones(".") }],
    },
  },
];
