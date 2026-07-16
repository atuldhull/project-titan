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

const TOKEN_MESSAGE =
  "Raw color values are forbidden in app code (TITAN-FRT-010) — use semantic tokens (bg-primary, text-destructive, var(--…)).";

export const TOKEN_SELECTORS = [
  { selector: `Literal[value=/${RAW_COLOR}/]`, message: TOKEN_MESSAGE },
  { selector: `TemplateElement[value.raw=/${RAW_COLOR}/]`, message: TOKEN_MESSAGE },
];

/**
 * TITAN-FRT-014: server state flows through @titan/api-client + React Query, not
 * ad-hoc fetch. `fetch` is banned in app code; it is allowed only in the BFF
 * route handlers (app/api/**) and the browser→BFF client (lib/bff/**), which the
 * app config re-enables via an override.
 */
const FETCH_MESSAGE =
  "Direct fetch is banned in app code (TITAN-FRT-014) — use @titan/api-client via React Query, or a BFF route handler / lib/bff.";

export const FETCH_SELECTORS = [
  { selector: "CallExpression[callee.name='fetch']", message: FETCH_MESSAGE },
  {
    selector:
      "MemberExpression[property.name='fetch'][object.name='window']",
    message: FETCH_MESSAGE,
  },
  {
    selector:
      "MemberExpression[property.name='fetch'][object.name='globalThis']",
    message: FETCH_MESSAGE,
  },
];

/** Build a `no-restricted-syntax` rule entry from one or more selector groups. */
export function restrictedSyntax(...selectorGroups) {
  return { "no-restricted-syntax": ["error", ...selectorGroups.flat()] };
}

/** Back-compat: the token-only rule object. */
export const tokenRule = restrictedSyntax(TOKEN_SELECTORS);

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
