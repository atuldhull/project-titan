import { expect, test } from "@playwright/test";

import { mockApi, unmatchedRoutes, FIXTURE_IDS } from "./support/api";
import { signIn } from "./support/session";
import { allRoutes, SCREENS } from "./support/screens";

/**
 * Not an assertion suite — a census. It walks every screen and reports which API
 * paths reached the mock with no fixture, so the fixture set is built from what
 * the app actually requests rather than from what I assumed it requests.
 *
 * Kept in the repo (skipped by default) because it is the tool for re-deriving
 * the fixture set whenever a screen's data needs change.
 */
test.skip(
  !process.env.TITAN_E2E_DISCOVER,
  "set TITAN_E2E_DISCOVER=1 to run the fixture census",
);

test("census: which endpoints does each screen call?", async ({
  page,
}, testInfo) => {
  testInfo.setTimeout(180_000);
  await signIn(page.context());
  await mockApi(page);
  await page.routeWebSocket(/.*/, () => {});

  const report: string[] = [];
  for (const route of allRoutes(FIXTURE_IDS.project)) {
    const before = new Set(unmatchedRoutes(page));
    await page.goto(route.url);
    await page.waitForLoadState("networkidle").catch(() => {});
    // Generous: on a cold `next start` the first render of each route compiles
    // and paints before React Query has fired anything, and a short wait here
    // reports "all fixtures matched" for a screen that simply had not asked yet.
    await page.waitForTimeout(2000);
    const added = unmatchedRoutes(page).filter((r) => !before.has(r));
    report.push(`\n### ${route.name} (${route.url})`);
    report.push(...(added.length ? added : ["  (all fixtures matched)"]));
  }
  await testInfo.attach("endpoint-census", { body: report.join("\n") });
  console.log(report.join("\n"));
  expect(SCREENS.length).toBe(13);
});
