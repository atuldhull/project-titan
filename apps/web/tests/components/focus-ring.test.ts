import { readFile } from "node:fs/promises";
import path from "node:path";

import { describe, expect, it } from "vitest";
import { compile } from "tailwindcss";

import { FOCUS_RING, TOUCH_TARGET_PSEUDO } from "@/components/ui/focus-ring";

/**
 * TITAN-FRT-042 / §9 — the shared focus ring must actually PAINT.
 *
 * This test compiles `FOCUS_RING` through the real installed Tailwind rather
 * than asserting on the class string, because the defect it guards is invisible
 * at the string level and was shipped once already:
 *
 *   .outline-none             { --tw-outline-style: none; outline-style: none }
 *   .focus-visible\:outline-2 { outline-style: var(--tw-outline-style); ... }
 *
 * Tailwind v4's width utilities defer `outline-style` to `--tw-outline-style`,
 * and `outline-none` sets that property to `none` UNCONDITIONALLY — not scoped
 * to a state. So `outline-none focus-visible:outline-2` compiles to a 2px
 * outline of style `none`: no ring at all, on every primitive, while also
 * outranking the base-layer `:focus-visible` fallback in globals.css. A string
 * assertion (`expect(FOCUS_RING).toContain("outline-2")`) passes happily
 * through that bug, which is exactly why this one goes to the compiler.
 *
 * Re-broken to confirm it is not vacuous: deleting `focus-visible:outline-solid`
 * from FOCUS_RING fails the "restores the style" assertion; deleting
 * `focus-visible:outline-ring` fails the colour assertion; swapping
 * `outline-offset-2` for `outline-offset-0` fails the §9 offset assertion.
 */

/** Compile an arbitrary class list against a theme that defines `--color-ring`. */
async function build(classes: readonly string[]): Promise<string> {
  const root = process.cwd();
  const compiler = await compile(
    '@import "tailwindcss" source(none);\n@theme { --color-ring: var(--ring); }',
    {
      base: root,
      loadStylesheet: async (id: string, base: string) => {
        const specifier = id === "tailwindcss" ? "tailwindcss/index.css" : id;
        const resolved = specifier.startsWith(".")
          ? path.resolve(base, specifier)
          : path.resolve(root, "node_modules", specifier);
        return {
          path: resolved,
          base: path.dirname(resolved),
          content: await readFile(resolved, "utf8"),
        };
      },
    },
  );
  // Collapse whitespace so the assertions below can be written as flat regexes
  // over nested (`&:focus-visible { … }`) output.
  return compiler.build([...classes]).replace(/\s+/g, " ");
}

/** The declarations Tailwind emits inside `.<cls> { &:focus-visible { … } }`. */
function focusVisibleBlock(css: string, cls: string): string {
  const escaped = cls.replace(/[:\\]/g, "\\$&").replace(/\\\\:/g, "\\\\:");
  const match = new RegExp(
    `\\.${escaped.replace(/:/g, "\\\\:")}\\s*\\{\\s*&:focus-visible\\s*\\{([^}]*)\\}`,
  ).exec(css);
  return match?.[1] ?? "";
}

describe("FOCUS_RING", () => {
  it("does not leave outline-style resolving to none", async () => {
    const classes = FOCUS_RING.split(" ");
    const css = await build(classes);

    // Establish the hazard is real and present: something in the string sets
    // --tw-outline-style to `none` outside any state.
    const suppressesStyle =
      /\.outline-none\s*\{[^}]*--tw-outline-style:\s*none/.test(css);
    expect(suppressesStyle).toBe(true);

    // Therefore a :focus-visible-scoped rule MUST put a real style back, or the
    // ring is 2px of nothing. `:focus-visible` outranks the bare `.outline-none`
    // on specificity, so this genuinely wins when the control is focused.
    expect(focusVisibleBlock(css, "focus-visible:outline-solid")).toMatch(
      /--tw-outline-style:\s*solid/,
    );
  });

  it("paints --ring at 2px with a 2px offset (§9)", async () => {
    const css = await build(FOCUS_RING.split(" "));

    expect(focusVisibleBlock(css, "focus-visible:outline-2")).toMatch(
      /outline-width:\s*2px/,
    );
    expect(focusVisibleBlock(css, "focus-visible:outline-offset-2")).toMatch(
      /outline-offset:\s*2px/,
    );
    expect(focusVisibleBlock(css, "focus-visible:outline-ring")).toMatch(
      /outline-color:\s*var\(--color-ring\)/,
    );
  });

  it("keeps the user-agent ring suppressed when NOT focus-visible", async () => {
    // The other half of the trade: `outline-none` is what stops Safari drawing
    // its own ring on plain `:focus` (mouse click). If a future edit "fixes"
    // this file by simply deleting `outline-none`, the ring works but the mouse
    // artefact comes back — that is a different change, not a cleanup.
    const css = await build(FOCUS_RING.split(" "));
    expect(css).toMatch(/\.outline-none\s*\{[^}]*outline-style:\s*none/);
  });

  it("reserves layout space equal to the hit area it claims", async () => {
    // §8. TOUCH_TARGET_PSEUDO grows what a 16 px checkbox CATCHES via a
    // pseudo-element. A pseudo-element is painted outside the host's layout
    // box, so without a matching margin the control catches 44 px while still
    // occupying 16 px — neighbours do not move, the expanded areas overlap, and
    // the lower control (painted later) steals taps aimed at the one above it.
    //
    // So the reserved margin must equal the pseudo-element's outset. Comparing
    // the two COMPILED lengths rather than eyeballing the class names is what
    // makes this catch the realistic regression: someone changing `-inset-3.5`
    // to `-inset-4` for a bigger target and not touching the margin.
    // Only TOUCH_TARGET_PSEUDO's own classes are compiled here, so there is
    // exactly one `margin:` and one `inset:` in the output — matching the
    // declarations directly avoids depending on Tailwind's selector escaping
    // (`.max-md\:before\:-inset-3\.5`), which is not what this is testing.
    const css = await build(TOUCH_TARGET_PSEUDO.split(" "));

    const margin = /margin: calc\(var\(--spacing\) \* ([\d.]+)\)/.exec(css);
    const inset = /inset: calc\(var\(--spacing\) \* -([\d.]+)\)/.exec(css);

    expect(margin?.[1]).toBeDefined();
    expect(inset?.[1]).toBeDefined();
    expect(margin?.[1]).toBe(inset?.[1]);
  });

  it("resolves --color-ring from the app theme, so outline-ring is not dead", async () => {
    // `outline-ring` compiles to `outline-color: var(--color-ring)`, which is
    // silently transparent-ish if the token was never mapped. globals.css owns
    // that mapping; this pins the two together.
    const globals = await readFile(
      path.resolve(process.cwd(), "src/app/globals.css"),
      "utf8",
    );
    expect(globals).toMatch(/--color-ring:\s*var\(--ring\)/);
  });
});
