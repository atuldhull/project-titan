import type { Page, Route } from "@playwright/test";

import { API_FIXTURES } from "@/lib/fixtures/api";

export { FIXTURE_IDS } from "@/lib/fixtures/api";

/**
 * Playwright wiring for the hermetic E2E sweep.
 *
 * The fixture bodies themselves live in `src/lib/fixtures/api.ts`, shared with
 * the Storybook harness so a story and a spec cannot disagree about what the
 * API returns. This module only binds that corpus to `page.route`.
 *
 * Unmatched paths are NOT silently emptied — they fulfil with a 599 and are
 * recorded, so a screen quietly rendering its error state shows up as a missing
 * fixture rather than as a passing test. `e2e/discovery.spec.ts` re-derives the
 * list of what each screen calls.
 */

/** Paths that reached the mock without a matching rule, per page. */
const unmatched = new WeakMap<Page, Set<string>>();

export function unmatchedRoutes(target: Page): string[] {
  return [...(unmatched.get(target) ?? [])].sort();
}

export async function mockApi(target: Page): Promise<void> {
  unmatched.set(target, new Set());
  await target.route("**/api/titan/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api\/titan/, "");
    const method = route.request().method();

    const rule = API_FIXTURES.find(
      (candidate) =>
        candidate.pattern.test(path) && (candidate.method ?? "GET") === method,
    );
    if (!rule) {
      unmatched.get(target)?.add(`${method} ${path}`);
      await route.fulfill({
        status: 599,
        contentType: "application/problem+json",
        body: JSON.stringify({
          title: "No E2E fixture",
          status: 599,
          detail: `${method} ${path}`,
        }),
      });
      return;
    }
    await route.fulfill({
      status: rule.status ?? 200,
      contentType: "application/json",
      body: JSON.stringify(rule.body),
    });
  });
}
