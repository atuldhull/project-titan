// Bundle budget gate (TITAN-FRT-005). Run after `next build`.
//
// The requirement has TWO clauses, and until Phase 66 this script enforced only
// the first:
//
//   1. "First Load JS for the Organization Overview route SHALL be <= 300 kB
//      gzipped"  -> the byte total below.
//   2. "heavy visualizations (graphs, charts, terminals) SHALL be dynamically
//      imported"  -> the HEAVY_MODULES scan below.
//
// Clause 2 was unguarded, and not hypothetically: dagre — half of ADR-FRT-04's
// "React Flow + dagre" pair — had escaped the dynamic boundary and sat in the
// First Load JS of /projects/[pid]/graph and /projects/[pid]/architecture at
// 30.8 kB gzipped each. A gate that measures only /org can never see that,
// because nothing on /org imports a graph. Clause 2 is therefore checked
// per-module across EVERY route rather than as a byte total on one.
import { readFileSync, readdirSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { join } from "node:path";

const NEXT_DIR = ".next";
const BUDGET_KB = Number(process.env.ORG_FIRST_LOAD_BUDGET_KB ?? 300);

/**
 * Modules that must never appear in any route's First Load JS.
 *
 * Matched against built chunk text, so each pattern is an INTERNAL identifier
 * that survives minification — not the package name, which does not reliably.
 * `network-simplex` and `acyclicer` are dagre's own layout internals and cannot
 * come from anything else.
 */
const HEAVY_MODULES = [
  { name: "@xyflow/react (React Flow)", pattern: /xyflow|reactflow/i },
  { name: "dagre", pattern: /network-simplex|acyclicer/i },
];

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

/**
 * Gzipped size of one built chunk.
 *
 * Exits rather than returning 0. The previous version swallowed a read failure
 * and contributed zero bytes, so a manifest-shape change, a relocated distDir,
 * or simply running from the wrong cwd would have made the gate report a small
 * number and print PASS. A budget check that cannot find the files it is
 * measuring has failed, not passed.
 */
function gzipBytes(file) {
  try {
    return gzipSync(readFileSync(join(NEXT_DIR, file))).length;
  } catch (error) {
    console.error(
      `FAIL: could not read chunk ${file} under ${NEXT_DIR} — the gate cannot ` +
        `measure what it cannot open (${error.message}).`,
    );
    process.exit(1);
  }
}

const sourceCache = new Map();
function chunkText(file) {
  if (!sourceCache.has(file)) {
    try {
      sourceCache.set(file, readFileSync(join(NEXT_DIR, file), "utf8"));
    } catch {
      sourceCache.set(file, "");
    }
  }
  return sourceCache.get(file);
}

const manifest = loadManifest();
const pages = manifest.pages ?? {};

// ---------------------------------------------------------------- clause 1

const routeKey = Object.keys(pages).find(
  (key) => key === "/org" || key.endsWith("/org/page") || key.endsWith("/org"),
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
if (files.length === 0) {
  console.error("FAIL: /org resolved to zero JS chunks — the gate is blind.");
  process.exit(1);
}
const totalBytes = files.reduce((sum, file) => sum + gzipBytes(file), 0);

// kB = 1000 bytes, NOT 1024.
//
// This is what `next build` prints, and the requirement is written against that
// output ("`next build` budget check in CI"). Dividing by 1024 — as this script
// used to — reports the same bytes as a ~2.4% smaller number, so the gate
// admitted 307,200 bytes while the build table beside it showed a route over
// the 300 kB the gate claims to enforce. Verified against Next 15.5.20: /org
// measured 229.2 KiB here and printed as "234 kB" there; 229.2 * 1024 / 1000
// = 234.7. Same bytes, and only one of the two numbers is the requirement's.
const totalKb = totalBytes / 1000;

console.log(
  `/org First Load JS (gzip): ${totalKb.toFixed(1)} kB across ${files.length} chunks (budget ${BUDGET_KB} kB)`,
);

let failed = false;

if (totalKb > BUDGET_KB) {
  console.error(
    `FAIL: /org First Load JS ${totalKb.toFixed(1)} kB exceeds the ${BUDGET_KB} kB budget (TITAN-FRT-005).`,
  );
  failed = true;
} else {
  console.log("PASS: within the First Load JS budget.");
}

// ---------------------------------------------------------------- clause 2

const violations = [];
for (const [route, entries] of Object.entries(pages)) {
  const js = [...new Set(entries.filter((f) => f.endsWith(".js")))];
  for (const file of js) {
    const text = chunkText(file);
    for (const heavy of HEAVY_MODULES) {
      if (heavy.pattern.test(text)) {
        violations.push({ route, file, name: heavy.name });
      }
    }
  }
}

if (violations.length > 0) {
  console.error(
    "\nFAIL: heavy visualizations must be dynamically imported (TITAN-FRT-005).",
  );
  for (const v of violations) {
    console.error(
      `  ${v.name} is in the First Load JS of ${v.route} (${v.file})`,
    );
  }
  console.error(
    "  Import it through a `next/dynamic` boundary — see " +
      "components/graph/canvas.tsx and components/graph/graph-shell.tsx.",
  );
  failed = true;
} else {
  console.log(
    `PASS: none of ${HEAVY_MODULES.map((m) => m.name).join(", ")} appears in ` +
      `any route's First Load JS (${Object.keys(pages).length} routes checked).`,
  );
}

// A scan that matched nothing anywhere is indistinguishable from a scan whose
// patterns stopped matching. Prove each pattern still bites by finding its
// module in the build SOMEWHERE — as a lazy chunk, which is where it belongs.
const chunkDir = join(NEXT_DIR, "static", "chunks");
let builtChunks = [];
try {
  builtChunks = readdirSync(chunkDir).filter((f) => f.endsWith(".js"));
} catch {
  builtChunks = [];
}
for (const heavy of HEAVY_MODULES) {
  const found = builtChunks.some((f) => {
    try {
      return heavy.pattern.test(readFileSync(join(chunkDir, f), "utf8"));
    } catch {
      return false;
    }
  });
  if (!found) {
    console.error(
      `FAIL: ${heavy.name} was not found anywhere in the build. Either it is no ` +
        "longer a dependency (update HEAVY_MODULES) or its minified identifiers " +
        "changed and the pattern no longer matches — in which case the clause-2 " +
        "scan above passed vacuously.",
    );
    failed = true;
  }
}

process.exit(failed ? 1 : 0);
