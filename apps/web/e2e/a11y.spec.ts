import AxeBuilder from "@axe-core/playwright";

import { expect, test, settled } from "./support/fixtures";
import { FIXTURE_IDS } from "./support/api";
import { allRoutes } from "./support/screens";

/**
 * TITAN-FRT-042/043/046: zero serious or critical axe violations on every
 * screen, in BOTH themes (Vol 4 §9).
 *
 * Both themes matters because half of what axe checks here is contrast, and
 * TITAN ships two full token sets (`globals.css` `:root` and `.dark`). A single
 * -theme run would leave the other half of the palette unmeasured — and dark is
 * the DEFAULT theme, so it is the one most users see.
 *
 * Scoped to `wcag2a wcag2aa wcag21a wcag21aa` — the standard §9 names — rather
 * than axe's full rule set, so the gate matches the requirement instead of
 * drifting with axe's best-practice rules.
 */

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

const THEMES = [
  { name: "dark", className: "dark" },
  { name: "light", className: "" },
] as const;

for (const theme of THEMES) {
  test.describe(`axe · ${theme.name}`, () => {
    for (const route of allRoutes(FIXTURE_IDS.project)) {
      test(`${route.name}`, async ({ app }) => {
        await app.goto(route.url);
        await app.evaluate((className) => {
          document.documentElement.className = className;
        }, theme.className);
        await settled(app);

        const results = await new AxeBuilder({ page: app })
          .withTags(TAGS)
          .analyze();

        const blocking = results.violations.filter((violation) =>
          ["serious", "critical"].includes(violation.impact ?? ""),
        );
        expect(
          blocking.map((violation) => ({
            id: violation.id,
            impact: violation.impact,
            help: violation.help,
            nodes: violation.nodes.slice(0, 3).map((node) => ({
              html: node.html.slice(0, 160),
              // The summary carries the numbers — "expected 4.5:1, got 3.9:1",
              // and which element axe took the background from. Without it a
              // contrast failure is unactionable and the temptation is to
              // fiddle with tokens until it goes quiet.
              why: node.failureSummary?.replace(/\s+/g, " ").slice(0, 300),
            })),
          })),
        ).toEqual([]);
      });
    }
  });
}

test.describe("overlays axe never sees from a plain page load", () => {
  // Every route in the sweep above is scanned as loaded, so a dialog that only
  // exists after a keystroke is never in the DOM when axe runs. The palette's
  // combobox/listbox markup — the part most likely to be subtly wrong — was
  // therefore covered only by a comment claiming it followed APG.
  for (const theme of THEMES) {
    test(`the command palette is axe-clean · ${theme.name}`, async ({
      app,
    }) => {
      await app.goto("/org");
      await app.evaluate((className) => {
        document.documentElement.className = className;
      }, theme.className);
      await settled(app);

      await app.keyboard.press("ControlOrMeta+k");
      await expect(
        app.getByRole("combobox", { name: /command palette/i }),
      ).toBeFocused();
      // Type, so the listbox holds real options rather than the empty state.
      await app.keyboard.type("net");
      await expect(app.getByRole("option").first()).toBeVisible();

      const results = await new AxeBuilder({ page: app })
        .withTags(TAGS)
        .analyze();
      const blocking = results.violations.filter((violation) =>
        ["serious", "critical"].includes(violation.impact ?? ""),
      );
      expect(
        blocking.map((violation) => ({
          id: violation.id,
          nodes: violation.nodes.slice(0, 3).map((node) => ({
            html: node.html.slice(0, 160),
            why: node.failureSummary?.replace(/\s+/g, " ").slice(0, 300),
          })),
        })),
      ).toEqual([]);
    });
  }

  test("the ? overlay is axe-clean", async ({ app }) => {
    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("?");
    await expect(
      app.getByRole("dialog", { name: /keyboard shortcuts/i }),
    ).toBeVisible();

    const results = await new AxeBuilder({ page: app })
      .withTags(TAGS)
      .analyze();
    expect(
      results.violations
        .filter((violation) =>
          ["serious", "critical"].includes(violation.impact ?? ""),
        )
        .map((violation) => violation.id),
    ).toEqual([]);
  });
});

test.describe("§9 specifics axe cannot see", () => {
  test("there is exactly one main landmark, and skip-to-content reaches it", async ({
    app,
  }) => {
    // /settings/* used to nest a second <main> inside the shell's, which makes
    // "skip to main content" ambiguous for every landmark-navigating user.
    for (const url of ["/org", "/settings/profile", "/settings/members"]) {
      await app.goto(url);
      await settled(app);
      await expect(app.locator("main")).toHaveCount(1);
    }

    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("Tab");
    const skip = app.getByRole("link", { name: /skip to content/i });
    await expect(skip).toBeFocused();
    await skip.press("Enter");
    await expect(app.locator("main")).toBeFocused();
  });

  for (const url of ["/org", "/projects", "/settings/profile", "/logs"]) {
    test(`every focusable control on ${url} shows a visible focus ring`, async ({
      app,
    }) => {
      /*
       * The failure this catches is `focus-visible:outline-none` with no
       * replacement — invisible focus, which slips past every other automated
       * check because the element IS focusable and IS reachable.
       *
       * The first version called `querySelector` — SINGULAR — on one route, so
       * it inspected exactly one KPI-card link. Stripping the focus treatment
       * from every button in the application would not have moved it. It now
       * walks every focusable control on four representative screens and names
       * each offender.
       */
      await app.goto(url);
      await settled(app);

      const result = await app.evaluate(() => {
        const focusable = Array.from(
          document.querySelectorAll<HTMLElement>(
            'main a[href], main button:not([disabled]), main input:not([disabled]), main select, main textarea, main [tabindex]:not([tabindex="-1"])',
          ),
        ).filter((element) => {
          const box = element.getBoundingClientRect();
          return box.width > 0 && box.height > 0;
        });

        const bad: string[] = [];
        for (const element of focusable) {
          element.focus();
          if (document.activeElement !== element) {
            continue; // not actually focusable — not this test's business
          }
          const style = getComputedStyle(element);
          const outlined =
            style.outlineStyle !== "none" &&
            parseFloat(style.outlineWidth) >= 2 &&
            style.outlineColor !== "transparent";
          const ringed = style.boxShadow !== "none" && style.boxShadow !== "";
          if (!outlined && !ringed) {
            bad.push(
              `${element.tagName.toLowerCase()}.${element.className?.toString().slice(0, 50)} — outline:${style.outlineStyle}/${style.outlineWidth}`,
            );
          }
        }
        return { checked: focusable.length, bad: bad.slice(0, 5) };
      });

      // A screen with nothing focusable would otherwise pass vacuously.
      expect(result.checked).toBeGreaterThan(3);
      expect(result.bad).toEqual([]);
    });
  }

  test("prefers-reduced-motion stops animation but not data (TITAN-FRT-045)", async ({
    app,
  }) => {
    await app.emulateMedia({ reducedMotion: "reduce" });
    await app.goto("/projects");
    await settled(app);

    const animated = await app.evaluate(() => {
      const running: string[] = [];
      for (const element of Array.from(document.querySelectorAll("*"))) {
        const style = getComputedStyle(element);
        const duration = parseFloat(style.animationDuration) || 0;
        const transition = parseFloat(style.transitionDuration) || 0;
        // The global rule collapses these to 0.01ms rather than `none`, because
        // Radix drives unmount off animation end events and `none` would leave
        // closed dialogs mounted forever.
        if (duration > 0.05 || transition > 0.05) {
          running.push(
            `${element.tagName.toLowerCase()} ${style.animationDuration}/${style.transitionDuration}`,
          );
        }
      }
      return running.slice(0, 5);
    });
    expect(animated).toEqual([]);

    // …and the data is still there. "No animation" must not mean "no updates".
    await expect(app.getByText("Netflix Clone").first()).toBeVisible();
  });

  test("200% zoom loses no function (TITAN-FRT-046)", async ({ app }) => {
    // Emulated by halving the viewport at the same CSS pixel ratio, which is
    // what browser zoom does to layout.
    await app.setViewportSize({ width: 640, height: 480 });
    await app.goto("/projects");
    await settled(app);

    await expect(
      app.getByRole("link", { name: /new project/i }).first(),
    ).toBeVisible();
    const bodyOverflow = await app.evaluate(
      () => document.body.scrollWidth - document.documentElement.clientWidth,
    );
    expect(bodyOverflow).toBeLessThanOrEqual(0);
  });
});
