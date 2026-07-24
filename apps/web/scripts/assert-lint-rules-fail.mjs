// Proof runner (Phase 5 DoD): each violating fixture must FAIL its lint rule,
// with the expected rule id in the report. Green fixtures passing = red exit.
import { execFileSync } from "node:child_process";

function lint(files) {
  try {
    execFileSync(
      "npx",
      [
        "eslint",
        "--no-config-lookup",
        "--config",
        "lint-fixtures/eslint.config.mjs",
        "--format",
        "json",
        ...files,
      ],
      { encoding: "utf8", shell: process.platform === "win32" },
    );
    return { failed: false, ruleIds: [] };
  } catch (error) {
    const report = JSON.parse(error.stdout);
    return {
      failed: true,
      ruleIds: report.flatMap((f) => f.messages.map((m) => m.ruleId)),
    };
  }
}

const checks = [
  {
    name: "TITAN-FRT-010 token rule",
    files: ["lint-fixtures/components/kanban/token-violation.tsx"],
    rule: "no-restricted-syntax",
  },
  {
    name: "TITAN-FRT-049 boundary rule",
    files: ["lint-fixtures/components/kanban/boundary-violation.ts"],
    rule: "import/no-restricted-paths",
  },
  {
    name: "TITAN-FRT-014 no-fetch rule",
    files: ["lint-fixtures/components/kanban/fetch-violation.ts"],
    rule: "no-restricted-syntax",
  },
  {
    name: "TITAN-SEC-072 dangerouslySetInnerHTML ban",
    files: ["lint-fixtures/components/kanban/dangerous-html-violation.tsx"],
    rule: "no-restricted-syntax",
  },
];

let ok = true;
for (const check of checks) {
  const { failed, ruleIds } = lint(check.files);
  if (failed && ruleIds.includes(check.rule)) {
    console.log(`PROOF OK: ${check.name} fails on its fixture (${check.rule})`);
  } else {
    console.error(
      `PROOF BROKEN: ${check.name} did NOT fail as required (got: ${ruleIds.join(", ") || "clean pass"})`,
    );
    ok = false;
  }
}
process.exit(ok ? 0 : 1);
