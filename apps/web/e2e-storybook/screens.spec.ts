import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { REQUIRED_STATES, SCREEN_STORIES } from "../scripts/screen-stories.mjs";

/**
 * Visual regression over every screen's four states (TITAN-FRT-002).
 *
 * This suite carries a second job that matters more than the pixels: it is the
 * only thing that proves the 52 screen stories RENDER. `check-story-coverage`
 * reads the Storybook index, and a story lands in that index whether or not it
 * throws on mount — so coverage alone could be satisfied by 52 stories that all
 * crash. Every test here asserts the story produced real content before it
 * compares a screenshot.
 */

interface StoryEntry {
  readonly id: string;
  readonly title: string;
  readonly name: string;
  readonly type?: string;
}

function storyIndex(): StoryEntry[] {
  const path = join("storybook-static", "index.json");
  const raw = JSON.parse(readFileSync(path, "utf8")) as {
    entries?: Record<string, StoryEntry>;
  };
  return Object.values(raw.entries ?? {}).filter(
    (entry) => entry.type === undefined || entry.type === "story",
  );
}

const INDEX = storyIndex();

function findStory(title: string, name: string): StoryEntry {
  const entry = INDEX.find((s) => s.title === title && s.name === name);
  if (!entry) {
    throw new Error(
      `No story "${name}" under "${title}" in the built Storybook index. ` +
        `Run \`npm run build-storybook\` first.`,
    );
  }
  return entry;
}

/**
 * Open one story in isolation and wait for it to settle.
 *
 * `iframe.html` rather than the manager UI: the manager's sidebar and toolbar
 * are Storybook's chrome, and snapshotting them would make every baseline
 * sensitive to a Storybook upgrade rather than to our own screens.
 */
async function openStory(page: Page, id: string): Promise<void> {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));

  await page.goto(`/iframe.html?id=${encodeURIComponent(id)}&viewMode=story`);
  await page.waitForLoadState("networkidle");

  const root = page.locator("#storybook-root");
  await root.waitFor({ state: "attached", timeout: 15_000 });

  // Storybook renders its own error panel INSIDE the root when a story throws,
  // so "the root has content" is not enough on its own.
  await expect(page.locator(".sb-show-errordisplay")).toHaveCount(0);
  expect(errors, `story ${id} threw on mount`).toEqual([]);
}

for (const screen of SCREEN_STORIES) {
  test.describe(`§${screen.section} ${screen.title}`, () => {
    for (const state of REQUIRED_STATES) {
      test(`${state} renders and matches its snapshot`, async ({ page }) => {
        const story = findStory(screen.title, state);
        await openStory(page, story.id);

        const root = page.locator("#storybook-root");

        if (state === "Loading") {
          // Count the story's whole SUBTREE, and require more than one element.
          //
          // This assertion has now been written wrong twice, in opposite
          // directions, and both versions looked right:
          //
          // 1. `toBeEmpty()` — a TEXT assertion. A loading state is skeletons,
          //    which carry no text, so it failed seven screens for rendering
          //    exactly what they should.
          // 2. `childElementCount > 0` — structurally incapable of failing.
          //    `.storybook/preview.tsx` wraps every story in a decorator
          //    `<div class="bg-background text-foreground">`, so `#storybook-
          //    root` has exactly ONE child whatever the story does. A screen
          //    that renders nothing at all counts 1, identical to a live one:
          //    the check was measuring the decorator, not the screen.
          //
          // `querySelectorAll("*")` counts descendants at any depth, so the
          // wrapper contributes its 1 and everything above that came from the
          // screen. Hence `> 1` rather than `> 0` — a null-rendering screen
          // leaves precisely the wrapper behind.
          await expect
            .poll(
              async () =>
                root.evaluate(
                  (node: Element) => node.querySelectorAll("*").length,
                ),
              { timeout: 10_000 },
            )
            .toBeGreaterThan(1);
        } else {
          // The trap this closes: a screen whose queries all failed renders a
          // small error panel, and a screen that crashed renders nothing. Both
          // would sail past a bare screenshot comparison on first run, because
          // the baseline would simply record the broken output.
          await expect
            .poll(async () => (await root.innerText()).trim().length, {
              timeout: 10_000,
            })
            .toBeGreaterThan(10);
        }

        await expect(page).toHaveScreenshot(
          `${screen.section}-${state.toLowerCase()}.png`,
          { fullPage: true },
        );
      });
    }
  });
}
