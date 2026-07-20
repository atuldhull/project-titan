import { defineConfig, devices } from "@playwright/test";

/**
 * E2E configuration for the responsive + accessibility sweep (Phase 65).
 *
 * The suite is HERMETIC: every screen's data is served by `e2e/support/api.ts`
 * via `page.route`, and the session is a locally minted cookie. That is possible
 * because the dashboard fetches everything client-side through the same-origin
 * `/api/titan` proxy — `serverApi()` has no call sites — so nothing this suite
 * asserts depends on a running FastAPI, Postgres, or Ollama. Phase 66 adds the
 * offline-against-Compose run that TITAN-FRT-004 asks for; this one exists to
 * make layout and a11y regressions fail deterministically.
 *
 * Tests run against a PRODUCTION build. `next dev` injects a dev-overlay portal
 * and unminified error boundaries into the DOM, both of which axe walks — the
 * measurement has to be of the app we ship, not of the dev server.
 */
const PORT = Number(process.env.TITAN_E2E_PORT ?? 3100);
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  outputDir: ".playwright/results",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : 4,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  timeout: 30_000,
  expect: { timeout: 7_000 },
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    // The shortcut engine branches on Meta vs Control by platform; pinning the
    // emulated platform keeps `ControlOrMeta` deterministic across machines.
    ...devices["Desktop Chrome"],
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `npm run build && npm run start -- --port ${PORT}`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 300_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
