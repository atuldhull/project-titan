// Four-state story coverage gate (TITAN-FRT-002).
//
// "Every screen SHALL define and render distinct loading, empty, error, and
// populated states. Storybook story exists per state per screen."
//
// Nothing enforced the second half until Phase 66, and the first half had
// quietly rotted because of it: /deployments had no error branch at all and
// rendered a failed fetch as "No projects yet", /settings/providers had no
// empty branch, and the architecture viewer showed "This version has no
// components" when the artifact fetch failed. A screen cannot merge without its
// four states now, because this fails the build.
//
// Reads the BUILT Storybook index rather than parsing story source. `index.json`
// is what Storybook itself resolved — a story that throws on load, or whose
// title has a typo, is absent from it. Parsing `export const Loading` out of a
// file would happily count a story that does not run.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { REQUIRED_STATES, SCREEN_STORIES } from "./screen-stories.mjs";

const STATIC_DIR = process.env.STORYBOOK_STATIC ?? "storybook-static";

function loadIndex() {
  const path = join(STATIC_DIR, "index.json");
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    console.error(
      `Could not read ${path} — run \`npm run build-storybook\` before the ` +
        `coverage check.`,
    );
    process.exit(1);
  }
}

// The registry must not be able to shrink itself.
//
// SCREEN_STORIES defines both the requirement and the scope it is checked over,
// so without this the gate grades its own homework: delete the §6.8 entry and
// the check goes from "13 screens have four states" to "12 screens have four
// states" and stays green, having lost a screen rather than found a defect.
// Thirteen is the count Vol 4 §6 defines, and `e2e/support/screens.ts` pins the
// same number with two tests of its own — this is the third leg, and it has to
// run BEFORE the coverage loop so a truncated registry fails here rather than
// passing there.
const EXPECTED_SCREENS = 13;

if (SCREEN_STORIES.length !== EXPECTED_SCREENS) {
  console.error(
    `FAIL: the screen registry lists ${SCREEN_STORIES.length} screens, but Vol 4 ` +
      `§6 defines ${EXPECTED_SCREENS}. Coverage is measured over this list, so a ` +
      `shortened list would narrow the gate instead of failing it. Fix ` +
      `scripts/screen-stories.mjs — and if §6 genuinely changed, update ` +
      `EXPECTED_SCREENS here and the two pinning tests in e2e/support/screens.ts ` +
      `together.`,
  );
  process.exit(1);
}

const duplicateSections = SCREEN_STORIES.map((screen) => screen.section).filter(
  (section, i, all) => all.indexOf(section) !== i,
);
if (duplicateSections.length > 0) {
  // A duplicated section would keep the count at 13 while covering only 12
  // screens — the same hole the length check closes, reached from the other
  // side.
  console.error(
    `FAIL: duplicate section(s) in the screen registry: ` +
      `${[...new Set(duplicateSections)].join(", ")}. Each §6 screen must ` +
      `appear exactly once.`,
  );
  process.exit(1);
}

const index = loadIndex();
const entries = Object.values(index.entries ?? {});

if (entries.length === 0) {
  console.error(
    "FAIL: the Storybook index contains zero stories. The build produced an " +
      "index but nothing in it, so this check would pass vacuously.",
  );
  process.exit(1);
}

/** title -> Set of story names present in the build. */
const byTitle = new Map();
for (const entry of entries) {
  if (entry.type !== undefined && entry.type !== "story") {
    continue;
  }
  if (!byTitle.has(entry.title)) {
    byTitle.set(entry.title, new Set());
  }
  byTitle.get(entry.title).add(entry.name);
}

const problems = [];
for (const screen of SCREEN_STORIES) {
  const present = byTitle.get(screen.title);
  if (!present) {
    problems.push(
      `§${screen.section}: no stories at all under "${screen.title}" ` +
        `(expected ${REQUIRED_STATES.join(", ")})`,
    );
    continue;
  }
  const missing = REQUIRED_STATES.filter((state) => !present.has(state));
  if (missing.length > 0) {
    problems.push(
      `§${screen.section} "${screen.title}": missing ${missing.join(", ")}`,
    );
  }
}

const total = SCREEN_STORIES.length * REQUIRED_STATES.length;

if (problems.length > 0) {
  console.error(
    `FAIL: four-state story coverage (TITAN-FRT-002) — ${problems.length} of ` +
      `${SCREEN_STORIES.length} screens incomplete:\n`,
  );
  for (const problem of problems) {
    console.error(`  ${problem}`);
  }
  console.error(
    `\nEvery screen needs one story per state, named exactly ` +
      `${REQUIRED_STATES.join(" / ")}, under its "Screens/…" title. See ` +
      `src/lib/stories/harness.tsx for the harness that renders a screen in a ` +
      `given state, and scripts/screen-stories.mjs for the registry.`,
  );
  process.exit(1);
}

console.log(
  `PASS: all ${SCREEN_STORIES.length} screens define ${REQUIRED_STATES.length} ` +
    `states (${total} stories, TITAN-FRT-002).`,
);
