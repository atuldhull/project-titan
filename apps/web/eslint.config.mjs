import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

import { boundaryZones, tokenRule } from "./eslint-rules.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    // TITAN-FRT-010 (token-only colors) + TITAN-FRT-049 (feature boundaries).
    // Violating fixtures proving both rules fail live in lint-fixtures/
    // (run via `make lint-proofs`, excluded from the normal lint pass).
    rules: {
      ...tokenRule,
      "import/no-restricted-paths": [
        "error",
        { basePath: `${__dirname}/src`, zones: boundaryZones(".") },
      ],
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
    ],
  },
];

export default eslintConfig;
