import { expect, test as base, type Page } from "@playwright/test";

import { mockApi, unmatchedRoutes } from "./api";
import { signIn } from "./session";

/**
 * The shared setup every spec needs: a signed-in context, deterministic API
 * fixtures, and a WebSocket that neither connects nor spins.
 */

export const test = base.extend<{ app: Page }>({
  app: async ({ page, context }, use) => {
    await signIn(context);
    await mockApi(page);
    // The real socket points at :8000, which is not running. Left alone it
    // reconnects on a backoff for the whole test, and the shell renders its
    // "Offline" degradation state — a real state, but not the one a layout or
    // axe pass should be measuring. Intercepting holds it open and quiet.
    await page.routeWebSocket(/\/ws(\?|$)/, () => {});
    await use(page);

    // A screen that fell back to an error state because a fixture was missing
    // still LOOKS fine to a layout assertion. Failing here makes that loud.
    const missing = unmatchedRoutes(page);
    if (missing.length > 0) {
      throw new Error(
        `The app called endpoints with no E2E fixture, so this screen was ` +
          `measured in an error state:\n  ${missing.join("\n  ")}\n` +
          `Add them to e2e/support/api.ts (see e2e/discovery.spec.ts).`,
      );
    }
  },
});

export { expect } from "@playwright/test";

/**
 * Wait until the screen has settled, and PROVE it rendered something.
 *
 * Screens render skeletons first, and measuring a skeleton's layout proves
 * nothing about the real one — but the more dangerous case is a screen that
 * rendered nothing at all. A blank page has no overflow, no contrast failures
 * and no unnamed controls, so every assertion in this suite passes vacuously on
 * one. The heading check is what stops a route from being "green" because it
 * was empty: every §6 screen renders an <h1>.
 */
export async function settled(page: Page): Promise<void> {
  await page.waitForLoadState("domcontentloaded");
  await page
    .locator('[aria-busy="true"], [data-slot="skeleton"]')
    .first()
    .waitFor({ state: "detached", timeout: 5_000 })
    .catch(() => {
      /* Some screens legitimately never render a skeleton. */
    });
  // Deliberately "has real content", not "has an <h1>": the first version of
  // this guard demanded a heading and two screens legitimately render their
  // title elsewhere. The property that matters is only that the route rendered
  // SOMETHING, so a blank page cannot pass the sweep vacuously.
  await page
    .locator("main")
    .first()
    .waitFor({ state: "visible", timeout: 10_000 });
  await expect
    .poll(
      async () =>
        (
          await page
            .locator("main")
            .first()
            .innerText()
            .catch(() => "")
        ).trim().length,
      { timeout: 10_000 },
    )
    .toBeGreaterThan(20);
  await page.waitForTimeout(200);
}
