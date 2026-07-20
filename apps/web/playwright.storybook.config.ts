import { defineConfig, devices } from "@playwright/test";

/**
 * Visual regression over the built Storybook (TITAN-FRT-002).
 *
 * "Storybook story exists per state per screen; visual regression suite
 * snapshots all four states." `scripts/check-story-coverage.mjs` enforces the
 * first clause off the same build; this config is the second.
 *
 * **A separate config, not a second project on the main one.** The two suites
 * need different servers — the sweep runs `next build && next start`, this runs
 * a static file server over `storybook-static` — and Playwright's `webServer` is
 * global to a config, so sharing one would boot both servers for every run and
 * put a Next build in front of every snapshot. (This is not a second CI
 * pipeline: both are jobs in the existing `ci.yml`.)
 *
 * **Why the snapshot path has no OS segment.** Playwright names baselines per
 * platform by default, which would mean a Windows developer and an Ubuntu runner
 * could never share one. Instead the baselines are Linux-only and BOTH sides
 * render inside `mcr.microsoft.com/playwright:v1.61.1-noble` — `make
 * visual-web` and the `visual-web` CI job use the same image, so the fonts and
 * rasteriser that produced a baseline are the ones comparing against it.
 * Running `npx playwright test -c playwright.storybook.config.ts` on the host
 * is expected to differ and is not how this suite is run.
 */
const PORT = Number(process.env.STORYBOOK_PORT ?? 6007);
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e-storybook",
  outputDir: ".playwright/storybook-results",
  snapshotPathTemplate: "{testDir}/__snapshots__/{arg}{ext}",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : 4,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  timeout: 30_000,
  expect: {
    timeout: 7_000,
    toHaveScreenshot: {
      // NO pixel tolerance, deliberately.
      //
      // This started at `maxDiffPixelRatio: 0.01` to absorb anti-aliasing —
      // reasoning about cross-machine rendering noise that does not exist here,
      // because baselines and comparisons both run in the same pinned
      // `mcr.microsoft.com/playwright:v1.61.1-noble` image. The cost was a
      // suite that proved nothing: changing `--primary` to a garish green
      // still passed all 52, because on a full-page screenshot the accent
      // colour touches well under 1% of the pixels.
      //
      // With the default threshold that same mutation fails 11 of 52 —
      // measured, not estimated. Eleven rather than all of them is the gate
      // being precise: a skeleton, an error panel and most empty states paint
      // nothing primary-coloured, so they are genuinely unchanged. If this ever
      // flakes, the fix is to make the story deterministic — never to widen the
      // tolerance until the failure goes away.
      animations: "disabled",
      caret: "hide",
    },
  },
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "node scripts/serve-static.mjs storybook-static",
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
