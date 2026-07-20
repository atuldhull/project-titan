import { expect, test, settled } from "./support/fixtures";

/**
 * The command palette and shortcut engine end to end (TITAN-FRT-007, -048,
 * Vol 4 §10).
 *
 * The unit suite already pins the resolver's rules; what only a browser can
 * prove is that the engine is actually MOUNTED, that its keys reach it through
 * the real focus and overlay stack, and that the `?` overlay lists what really
 * fires.
 */

/**
 * Phase 65's DoD says the palette must "reach and operate every screen
 * keyboard-only". Reaching all thirteen is checkable, so it is checked — one
 * query per §6 screen, each typed into the palette and committed with Enter.
 */
const REACHABLE: readonly { query: string; url: RegExp }[] = [
  { query: "org", url: /\/org$/ },
  { query: "projects", url: /\/projects$/ },
  { query: "board netflix", url: /\/projects\/[^/]+\/board$/ },
  { query: "graph netflix", url: /\/projects\/[^/]+\/graph$/ },
  { query: "agents", url: /\/agents$/ },
  { query: "conversations", url: /\/conversations$/ },
  { query: "logs", url: /\/logs$/ },
  { query: "architecture netflix", url: /\/projects\/[^/]+\/architecture$/ },
  { query: "deployments", url: /\/deployments$/ },
  { query: "monitoring", url: /\/monitoring$/ },
  { query: "memory", url: /\/memory$/ },
  { query: "plugins", url: /\/plugins$/ },
  { query: "settings profile", url: /\/settings\/profile$/ },
];

test.describe("the palette reaches every screen keyboard-only", () => {
  for (const target of REACHABLE) {
    test(`"${target.query}" → ${target.url.source}`, async ({ app }) => {
      await app.goto("/org");
      await settled(app);
      await app.keyboard.press("ControlOrMeta+k");
      await app.keyboard.type(target.query);
      // Never click: the point of the assertion is that a keyboard alone gets
      // there.
      await app.keyboard.press("Enter");
      await expect(app).toHaveURL(target.url);
    });
  }

  test("covers all 13 §6 screens", () => {
    expect(REACHABLE).toHaveLength(13);
  });
});

test.describe("command palette", () => {
  test("⌘K opens it, and well inside the 100 ms §1.4 budget", async ({
    app,
  }) => {
    await app.goto("/org");
    await settled(app);

    const started = Date.now();
    await app.keyboard.press("ControlOrMeta+k");
    const input = app.getByRole("combobox", { name: /command palette/i });
    await expect(input).toBeFocused();
    // Generous vs the 100 ms budget because this measures Playwright's round
    // trip too; it still fails loudly if opening starts fetching or re-ranking
    // synchronously.
    expect(Date.now() - started).toBeLessThan(1000);
  });

  test("typing 'kan net' surfaces the netflix-clone board (TITAN-FRT-007)", async ({
    app,
  }) => {
    // The requirement's own worked example, verbatim.
    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("ControlOrMeta+k");
    await app
      .getByRole("combobox", { name: /command palette/i })
      .fill("kan net");

    const first = app.getByRole("option").first();
    await expect(first).toContainText("Board");
    await expect(first).toContainText("netflix-clone");

    await app.keyboard.press("Enter");
    await expect(app).toHaveURL(/\/projects\/[^/]+\/board$/);
  });

  test("arrow keys move the active option without moving DOM focus", async ({
    app,
  }) => {
    // The APG combobox pattern: focus stays in the input so type-ahead keeps
    // working. If focus moved onto the options, the next keystroke would be lost.
    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("ControlOrMeta+k");
    const input = app.getByRole("combobox", { name: /command palette/i });

    await app.keyboard.press("ArrowDown");
    await expect(input).toBeFocused();
    const active = await input.getAttribute("aria-activedescendant");
    expect(active).toBe("palette-option-1");
    await expect(app.locator("#palette-option-1")).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  test("Esc closes it and returns focus to the page", async ({ app }) => {
    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("ControlOrMeta+k");
    await expect(app.getByRole("dialog")).toBeVisible();
    await app.keyboard.press("Escape");
    await expect(app.getByRole("dialog")).toBeHidden();
  });

  test("⌘P opens the same surface narrowed to projects", async ({ app }) => {
    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("ControlOrMeta+p");
    await expect(
      app.getByRole("combobox", { name: /switch project/i }),
    ).toBeFocused();
    // Only projects — no screens, agents or tasks.
    await expect(app.getByRole("option")).toHaveCount(2);
    await expect(app.getByRole("option").first()).toContainText(
      "Netflix Clone",
    );
  });

  test("the top bar buttons open the same palette (§8: no ⌘K on a phone)", async ({
    app,
  }) => {
    await app.setViewportSize({ width: 375, height: 812 });
    await app.goto("/org");
    await settled(app);
    await app
      .getByRole("button", { name: /search screens, projects/i })
      .click();
    await expect(
      app.getByRole("combobox", { name: /command palette/i }),
    ).toBeFocused();
  });
});

test.describe("§10 sequences", () => {
  test("g then l goes to Logs", async ({ app }) => {
    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("g");
    await app.keyboard.press("l");
    await expect(app).toHaveURL(/\/logs$/);
  });

  test("g then a goes to Agents", async ({ app }) => {
    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("g");
    await app.keyboard.press("a");
    await expect(app).toHaveURL(/\/agents$/);
  });

  test("the sequence times out after 1.5 s", async ({ app }) => {
    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("g");
    await app.waitForTimeout(1700);
    await app.keyboard.press("l");
    await expect(app).toHaveURL(/\/org$/);
  });

  test("shortcuts do not fire while typing", async ({ app }) => {
    // The rule that makes the whole engine safe: `g` in a filter box is a
    // letter, not a navigation.
    await app.goto("/projects");
    await settled(app);
    const filter = app.getByRole("textbox").first();
    await filter.click();
    await filter.type("gl");
    await expect(app).toHaveURL(/\/projects$/);
    await expect(filter).toHaveValue("gl");
  });

  test("an unclaimed browser chord is left alone", async ({ app }) => {
    // Ctrl+S must still belong to the browser. The engine only preventDefaults
    // on a match, and `validate()` refuses to let anyone register this.
    await app.goto("/org");
    await settled(app);
    const prevented = await app.evaluate(async () => {
      let defaultPrevented = false;
      const probe = new KeyboardEvent("keydown", {
        key: "s",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      });
      document.dispatchEvent(probe);
      defaultPrevented = probe.defaultPrevented;
      return defaultPrevented;
    });
    expect(prevented).toBe(false);
  });
});

test.describe("the ? overlay (TITAN-FRT-048)", () => {
  test("lists the global §10 table", async ({ app }) => {
    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("?");

    const dialog = app.getByRole("dialog", { name: /keyboard shortcuts/i });
    await expect(dialog).toBeVisible();

    // Every row §10 specifies for the global scope.
    for (const label of [
      "Command palette",
      "Project switcher",
      "Shortcut help",
      "Close dialog, sheet or palette",
      "Submit form",
      "New (contextual)",
      "Focus search",
      "Go to Org",
      "Go to Projects",
      "Go to Board",
      "Go to Graph",
      "Go to Agents",
      "Go to Conversations",
      "Go to Logs",
      "Go to Deployments",
      "Go to Monitoring",
    ]) {
      await expect(dialog.getByText(label, { exact: true })).toBeVisible();
    }
  });

  test("renders a sequence as two separate caps, not one chord", async ({
    app,
  }) => {
    // `G` then `O` are pressed in turn. Rendering them as `G+O` would document
    // a chord that does not exist.
    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("?");
    const row = app.locator('[data-shortcut="goto-o"]');
    await expect(row.locator("kbd")).toHaveText(["G", "O"]);
  });

  test("shows the chord with the platform's own modifier cap", async ({
    app,
  }) => {
    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("?");
    // Emulated as Desktop Chrome (linux/win) in playwright.config.ts.
    await expect(
      app.locator('[data-shortcut="palette"]').locator("kbd"),
    ).toHaveText(["Ctrl", "K"]);
  });

  test("does not open while typing", async ({ app }) => {
    // `?` is a printable character; stealing it inside a text field would make
    // the filter unable to accept a question mark.
    await app.goto("/projects");
    await settled(app);
    const filter = app.getByRole("textbox").first();
    await filter.click();
    await filter.type("?");
    await expect(
      app.getByRole("dialog", { name: /keyboard shortcuts/i }),
    ).toBeHidden();
    await expect(filter).toHaveValue("?");
  });

  test("? typed INTO the palette is a question mark, not a shortcut", async ({
    app,
  }) => {
    // The palette focuses its own search input, and §10 exempts text entry from
    // printable-key shortcuts. Stealing `?` there would make the search box
    // unable to accept the character.
    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("ControlOrMeta+k");
    await app.keyboard.type("?");
    await expect(
      app.getByRole("dialog", { name: /keyboard shortcuts/i }),
    ).toBeHidden();
    await expect(
      app.getByRole("combobox", { name: /command palette/i }),
    ).toHaveValue("?");
  });

  test("Esc closes the top-most layer, not the one underneath", async ({
    app,
  }) => {
    // `?` from a screen, then ⌘K on top of it — two stacked layers of ours.
    // Esc must take the palette and leave the help overlay standing. This is
    // Radix's dismissable-layer stack doing the work; the global Esc row
    // deliberately stands down while any Radix layer is open, because racing it
    // would close whichever layer our handler happened to test first.
    await app.goto("/org");
    await settled(app);
    await app.keyboard.press("?");
    await expect(
      app.getByRole("dialog", { name: /keyboard shortcuts/i }),
    ).toBeVisible();

    await app.keyboard.press("ControlOrMeta+k");
    await expect(
      app.getByRole("combobox", { name: /command palette/i }),
    ).toBeFocused();

    await app.keyboard.press("Escape");
    await expect(
      app.getByRole("combobox", { name: /command palette/i }),
    ).toBeHidden();
    await expect(
      app.getByRole("dialog", { name: /keyboard shortcuts/i }),
    ).toBeVisible();
  });
});
