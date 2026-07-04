/**
 * Shared TITAN lint-rule fragments, used by BOTH the app eslint config and
 * the lint-fixtures config — the fixture proof exercises the exact same
 * rule objects that guard production code.
 */

/**
 * TITAN-FRT-010: components consume semantic CSS-variable tokens only.
 * Rejects raw hex colors and rgb()/hsl() literals in app code (strings and
 * template literals). Tokens themselves live in globals.css, which ESLint
 * does not lint.
 */
const RAW_COLOR =
  "(#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\\b|\\b(rgba?|hsla?)\\()";

export const tokenRule = {
  "no-restricted-syntax": [
    "error",
    {
      selector: `Literal[value=/${RAW_COLOR}/]`,
      message:
        "Raw color values are forbidden in app code (TITAN-FRT-010) — use semantic tokens (bg-primary, text-destructive, var(--…)).",
    },
    {
      selector: `TemplateElement[value.raw=/${RAW_COLOR}/]`,
      message:
        "Raw color values are forbidden in app code (TITAN-FRT-010) — use semantic tokens (bg-primary, text-destructive, var(--…)).",
    },
  ],
};

/**
 * TITAN-FRT-049: feature code stays in its feature directory; cross-feature
 * imports go through lib/ or packages/ only. components/ui and components/
 * shell are shared primitives/chrome, importable by every feature.
 */
export const featureDirs = [
  "kanban",
  "graph",
  "agents",
  "conversations",
  "logs",
  "deployments",
  "monitoring",
  "memory",
  "plugins",
  "notifications",
];

export function boundaryZones(base) {
  return featureDirs.map((feature) => ({
    target: `${base}/components/${feature}`,
    from: `${base}/components`,
    except: [`./${feature}`, "./ui", "./shell"],
    message: `Cross-feature import into components/${feature} (TITAN-FRT-049) — share via lib/ or packages/ instead.`,
  }));
}
