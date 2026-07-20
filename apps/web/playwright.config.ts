import { defineConfig, devices } from "@playwright/test";

/**
 * E2E configuration for the responsive + accessibility sweep (Phase 65).
 *
 * The suite is HERMETIC: every screen's data is served by `e2e/support/api.ts`
 * via `page.route`, and the session is a locally minted cookie. That is possible
 * because the dashboard fetches everything client-side through the same-origin
 * `/api/titan` proxy — `serverApi()` has no call sites — so nothing this suite
 * asserts depends on a running FastAPI, Postgres, or Ollama. This one exists to
 * make layout and a11y regressions fail deterministically.
 *
 * **TITAN-FRT-004 is still NOT covered, and Phase 66 did not deliver it.** The
 * requirement is a run against the local Docker Compose backend with the machine
 * offline except loopback. Four things block it today, each verified rather than
 * assumed:
 *
 *   1. The `web` Compose service cannot build. Its context is `./apps/web`, but
 *      the app resolves `@titan/{schemas,api-client,ws-protocol}` through
 *      tsconfig paths at `../../packages/*` — outside the context — and there is
 *      no root package.json or npm workspace to supply them.
 *      `infra/docker/titan-frontend.Dockerfile` has not been touched since
 *      Phase 2, before those packages existed.
 *   2. Nothing in Compose runs migrations. There is no migrate service and the
 *      API does not run alembic at startup, so a fresh stack has an empty
 *      schema — and `/readyz` only issues `SELECT 1`, so the api container
 *      reports HEALTHY against it and `web` waits on that health gate happily.
 *   3. There is no realistic seed. `apps/api/scripts/seed.py` inserts five roles
 *      and one admin user; no projects, tasks, agents or runs. Every screen
 *      would render its empty state, so a live run would assert nothing.
 *   4. This suite is architecturally the opposite of a live one: it mints an
 *      unsigned JWT that FastAPI would reject and `page.route`s every call.
 *      Pointing it at a backend is a rewrite of `e2e/support/{session,api}.ts`
 *      plus a real login, not a flag — best done as a second Playwright project.
 *
 * Worth noting what Ollama is and is not needed for: it is an opt-in
 * `--profile local-llm` service, the API does not depend on it at startup, and
 * exactly one of the 13 screens (§6.11's semantic search) forces an embedding
 * round-trip. "Offline via Ollama" is also false on a cold volume — the image
 * is unpinned and no model is pre-pulled, so the first run fetches ~5 GB.
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
