import { expect, test, settled } from "./support/fixtures";
import { FIXTURE_IDS } from "./support/api";
import { allRoutes, SCREENS } from "./support/screens";

/**
 * TITAN-FRT-041: all 13 screens usable at 375px, with NO horizontal body scroll
 * and all primary actions reachable (Vol 4 §8).
 *
 * The body-scroll assertion is the one that catches real regressions, and it has
 * a trap: `document.body.scrollWidth > clientWidth` is not enough on its own,
 * because this shell's scroll port is `<main>`, not `<body>`. A wide table can
 * therefore overflow visibly while the body stays clean. So the check walks
 * every element and reports the widest offender by name — a failure that says
 * "the projects table is 720px in a 375px viewport" is actionable; a bare
 * `expect(false)` is not.
 */

const MOBILE = { width: 375, height: 812 };

test.describe("375px", () => {
  test.use({ viewport: MOBILE });

  for (const route of allRoutes(FIXTURE_IDS.project)) {
    test(`${route.name} has no horizontal overflow`, async ({ app }) => {
      await app.goto(route.url);
      await settled(app);

      const overflow = await app.evaluate(() => {
        /*
         * The FIRST version of this walk asked "is this element inside any
         * ancestor with overflow-x auto/scroll?" and skipped it if so. That
         * made the whole assertion vacuous: the shell's `<main>` is
         * `overflow-y-auto`, and a computed `overflow-y` other than `visible`
         * forces `overflow-x` to compute to `auto` as well. Every element on
         * every screen therefore had a "scrollable ancestor" and every route
         * passed no matter how wide its content was.
         *
         * The real §8 rule is about WHICH box takes the scroll. Three boxes
         * must never scroll horizontally — the document, the body, and the
         * shell's `<main>` content region — because scrolling any of them is
         * the "horizontal body scroll" TITAN-FRT-041 forbids. A purpose-built
         * scroller INSIDE the content (the Kanban column strip, a table
         * wrapper, the settings nav) is exactly what §8 asks for, so those are
         * allowed to overflow and are reported separately for information.
         */
        const page: Element[] = [document.documentElement, document.body];
        const main = document.querySelector("main");
        if (main) {
          page.push(main);
        }

        const pageScroll = page
          .map((element) => ({
            tag: element.tagName.toLowerCase(),
            overflowBy: element.scrollWidth - element.clientWidth,
          }))
          .filter((entry) => entry.overflowBy > 1);

        // An element wider than the viewport whose nearest scrollable ancestor
        // is one of those three is pushing the PAGE sideways, not scrolling in
        // a container of its own — that is the offender §8 means.
        const isOwnScroller = (element: Element) =>
          /(auto|scroll)/.test(getComputedStyle(element).overflowX) &&
          !page.includes(element);

        const offenders: { tag: string; right: number; text: string }[] = [];
        const viewport = document.documentElement.clientWidth;
        for (const element of Array.from(document.body.querySelectorAll("*"))) {
          const box = element.getBoundingClientRect();
          if (box.width === 0 && box.height === 0) {
            continue;
          }
          if (box.right <= viewport + 1) {
            continue;
          }
          const style = getComputedStyle(element);
          if (style.position === "fixed" || style.visibility === "hidden") {
            continue;
          }
          let contained = false;
          for (
            let parent = element.parentElement;
            parent;
            parent = parent.parentElement
          ) {
            if (isOwnScroller(parent)) {
              contained = true;
              break;
            }
            if (page.includes(parent)) {
              break;
            }
          }
          if (contained) {
            continue;
          }
          offenders.push({
            tag: `${element.tagName.toLowerCase()}.${element.className?.toString().slice(0, 60)}`,
            right: Math.round(box.right),
            text: (element.textContent ?? "").trim().slice(0, 40),
          });
        }
        return {
          pageScroll,
          worst: offenders.sort((a, b) => b.right - a.right).slice(0, 3),
        };
      });

      expect(
        overflow.pageScroll,
        "the document, body and <main> must never scroll horizontally (§8)",
      ).toEqual([]);
      expect(
        overflow.worst,
        `elements extend past ${MOBILE.width}px without a scroll container of their own`,
      ).toEqual([]);
    });
  }

  test("the shell swaps the sidebar for a bottom tab bar (§8)", async ({
    app,
  }) => {
    await app.goto("/org");
    await settled(app);

    await expect(app.locator("aside")).toBeHidden();
    const tabs = app.getByRole("navigation", { name: "Sections" });
    await expect(tabs).toBeVisible();
    // §8 names the five slots exactly.
    for (const label of ["Org", "Board", "Agents", "Convos", "More"]) {
      await expect(tabs.getByText(label, { exact: true })).toBeVisible();
    }
  });

  test("the bottom bar does not cover the end of the page", async ({ app }) => {
    // Padding has to be on <main> (the scroll port), not <body>, or the last
    // row of every screen sits under the bar and cannot be reached.
    await app.goto("/projects");
    await settled(app);
    const clearance = await app.evaluate(() => {
      const main = document.querySelector("main");
      const bar = document.querySelector('nav[aria-label="Sections"]');
      if (!main || !bar) {
        return null;
      }
      return {
        padding: parseFloat(getComputedStyle(main).paddingBottom),
        barHeight: bar.getBoundingClientRect().height,
      };
    });
    expect(clearance).not.toBeNull();
    expect(clearance!.padding).toBeGreaterThanOrEqual(clearance!.barHeight);
  });

  test("primary actions stay reachable and large enough to hit", async ({
    app,
  }) => {
    await app.goto("/projects");
    await settled(app);
    const newProject = app.getByRole("link", { name: /new project/i }).first();
    await expect(newProject).toBeVisible();
    const box = await newProject.boundingBox();
    // §8: touch targets ≥ 44px under md.
    expect(box!.height).toBeGreaterThanOrEqual(44);
  });

  test("the viewport allows pinch-zoom (§9, TITAN-FRT-046)", async ({
    app,
  }) => {
    // A `maximum-scale` or `user-scalable=no` here would fail WCAG 1.4.4
    // outright, and it is the kind of thing added casually to "fix" a mobile
    // layout. Next injects a sane default today; this keeps it that way.
    await app.goto("/org");
    const content = await app
      .locator('meta[name="viewport"]')
      .getAttribute("content");
    expect(content ?? "").not.toMatch(/user-scalable\s*=\s*no/);
    expect(content ?? "").not.toMatch(/maximum-scale\s*=\s*1/);
  });
});

test.describe("the §8 breakpoint bands", () => {
  test("768–1279 shows an icon rail, ≥1280 the full sidebar", async ({
    app,
  }) => {
    await app.setViewportSize({ width: 1024, height: 900 });
    await app.goto("/org");
    await settled(app);
    const rail = await app.locator("aside").boundingBox();
    expect(rail!.width).toBeLessThanOrEqual(80);

    await app.setViewportSize({ width: 1440, height: 900 });
    await settled(app);
    const full = await app.locator("aside").boundingBox();
    expect(full!.width).toBeGreaterThanOrEqual(200);
  });

  test("the icon rail keeps its links named for a screen reader", async ({
    app,
  }) => {
    // The visible label is display:none in rail mode, which also removes it
    // from the a11y tree — so an unnamed rail would still LOOK fine.
    await app.setViewportSize({ width: 1024, height: 900 });
    await app.goto("/org");
    await settled(app);
    const sidebar = app.getByRole("navigation", { name: "Primary" });
    await expect(
      sidebar.getByRole("link", { name: "Monitoring" }),
    ).toBeVisible();
  });
});

test("all 13 §6 screens are covered by this sweep", () => {
  // TITAN-FRT-041 is written against thirteen. If a fourteenth screen lands
  // without an entry here, the suite would silently stop covering it.
  expect(SCREENS).toHaveLength(13);
});
