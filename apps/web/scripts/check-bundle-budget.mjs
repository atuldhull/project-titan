// Bundle budget gate (TITAN-FRT-005): the Organization Overview (/org) First
// Load JS must stay <= 300 kB gzipped. Run after `next build`; sums the gzipped
// size of the route's JS chunks from the App Router build manifest and fails CI
// when the budget is exceeded. Heavy visualizations must be dynamically imported
// to stay under it.
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { join } from "node:path";

const NEXT_DIR = ".next";
const BUDGET_KB = Number(process.env.ORG_FIRST_LOAD_BUDGET_KB ?? 300);

function loadManifest() {
  const path = join(NEXT_DIR, "app-build-manifest.json");
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    console.error(
      `Could not read ${path} — run \`next build\` before the budget check.`,
    );
    process.exit(1);
  }
}

function gzipBytes(file) {
  try {
    return gzipSync(readFileSync(join(NEXT_DIR, file))).length;
  } catch {
    return 0;
  }
}

const manifest = loadManifest();
const pages = manifest.pages ?? {};
const routeKey = Object.keys(pages).find(
  (key) => key === "/org" || key.includes("/org/page") || key.endsWith("/org"),
);

if (!routeKey) {
  console.error(
    `Could not locate the /org route in the build manifest. Routes:\n  ${Object.keys(
      pages,
    ).join("\n  ")}`,
  );
  process.exit(1);
}

const files = [...new Set(pages[routeKey].filter((f) => f.endsWith(".js")))];
const totalBytes = files.reduce((sum, file) => sum + gzipBytes(file), 0);
const totalKb = totalBytes / 1024;

console.log(
  `/org First Load JS (gzip): ${totalKb.toFixed(1)} kB across ${files.length} chunks (budget ${BUDGET_KB} kB)`,
);

if (totalKb > BUDGET_KB) {
  console.error(
    `FAIL: /org First Load JS ${totalKb.toFixed(1)} kB exceeds the ${BUDGET_KB} kB budget (TITAN-FRT-005).`,
  );
  process.exit(1);
}
console.log("PASS: within the First Load JS budget.");
