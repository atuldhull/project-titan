"use client";

import { useRouter, usePathname } from "next/navigation";

import { useShortcuts } from "@/lib/shortcuts/provider";
import { isOverlayOpen } from "@/lib/shortcuts/keys";
import type { Shortcut } from "@/lib/shortcuts/registry";
import { usePrimarySearchFocus } from "@/lib/shortcuts/primary-search";
import { usePaletteStore } from "@/stores/palette";
import { useShortcutsOverlay } from "@/components/shell/shortcuts-overlay";

/**
 * The global half of Vol 4 §10 — registered once, in the shell.
 *
 * Every row §10 lists is here, including the two that are "contextual" (`n`,
 * `/`). Those get a sensible default rather than being left to individual
 * screens, because TITAN-FRT-048 requires the `?` overlay to match the table:
 * a binding that exists on four screens and is absent from the overlay on the
 * other nine is a table that does not match. Screens specialise by registering
 * the SAME id — `provider.tsx` lets a later registration shadow an earlier one.
 */

/** §10's Gmail-style sequences, in the order the spec lists them. */
const GO_TO: readonly { key: string; label: string; href: string }[] = [
  { key: "o", label: "Go to Org", href: "/org" },
  { key: "p", label: "Go to Projects", href: "/projects" },
  { key: "a", label: "Go to Agents", href: "/agents" },
  { key: "c", label: "Go to Conversations", href: "/conversations" },
  { key: "l", label: "Go to Logs", href: "/logs" },
  { key: "d", label: "Go to Deployments", href: "/deployments" },
  { key: "m", label: "Go to Monitoring", href: "/monitoring" },
];

export function GlobalShortcuts() {
  const router = useRouter();
  const pathname = usePathname();
  const palette = usePaletteStore();
  const overlay = useShortcutsOverlay();
  const focusSearch = usePrimarySearchFocus();

  // `g b` and `g g` are project-scoped: §10 lists them as Board and Graph, and
  // neither exists without a project. Outside one, they route to the picker
  // rather than doing nothing — a dead key reads as a bug.
  const pid = /^\/projects\/([^/]+)/.exec(pathname)?.[1];
  const projectHref = (suffix: string) =>
    pid ? `/projects/${pid}/${suffix}` : "/projects";

  const shortcuts: Shortcut[] = [
    // The four rows below carry `allowBehindOverlay` because the engine now
    // suppresses bindings while ANY modal is open (so `g l` cannot navigate the
    // page under a dialog). These four are exactly the ones that must survive
    // it: ⌘K has to be able to CLOSE the palette it opened, `?` has to reach
    // the help overlay from on top of it, and ⌘Enter has to submit the form
    // inside a dialog — which is the only place most forms live.
    {
      id: "palette",
      keys: "Mod+k",
      label: "Command palette",
      group: "Global",
      allowBehindOverlay: true,
      run: () => palette.toggle("commands"),
    },
    {
      id: "project-switcher",
      keys: "Mod+p",
      label: "Project switcher",
      group: "Global",
      allowBehindOverlay: true,
      run: () => palette.toggle("projects"),
    },
    {
      id: "help",
      keys: "?",
      label: "Shortcut help",
      group: "Global",
      allowBehindOverlay: true,
      run: () => overlay.toggle(),
    },
    {
      id: "close",
      keys: "Escape",
      label: "Close dialog, sheet or palette",
      group: "Global",
      allowInTextEntry: true,
      allowBehindOverlay: true,
      run: () => {
        // "Top-most first" is Radix's job, not ours. Every one of our layers is
        // a Radix Dialog or Sheet whose `onOpenChange` writes back into the
        // store, and Radix's dismissable-layer stack already closes the FOCUSED
        // one. Racing it from here would close whichever layer this handler
        // happens to check first — which, with the palette open under the help
        // overlay, is the wrong one.
        //
        // The row still exists because §10 lists Esc and TITAN-FRT-048 requires
        // the overlay to list it; it acts only when no Radix layer is up.
        if (isOverlayOpen(document)) {
          return;
        }
        if (overlay.open) {
          overlay.setOpen(false);
        } else if (palette.mode !== null) {
          palette.close();
        }
      },
    },
    {
      id: "submit",
      keys: "Mod+Enter",
      label: "Submit form",
      group: "Global",
      allowInTextEntry: true,
      allowBehindOverlay: true,
      run: () => {
        const active = document.activeElement;
        if (!(active instanceof HTMLElement)) {
          return;
        }
        // `requestSubmit` runs validation and fires `submit`; `form.submit()`
        // would skip both, and `.click()` on a button is defeated by anything
        // that gates the button on a typed confirmation.
        active.closest("form")?.requestSubmit();
      },
    },
    {
      id: "new",
      keys: "n",
      label: "New (contextual)",
      group: "Global",
      run: () => router.push("/projects/new"),
    },
    {
      id: "search",
      keys: "/",
      label: "Focus search",
      group: "Global",
      run: () => {
        if (focusSearch) {
          focusSearch();
        } else {
          // No search on this screen — the palette IS one, so the key still
          // does the thing the user meant.
          palette.open("commands");
        }
      },
    },
    ...GO_TO.map((target): Shortcut => ({
      id: `goto-${target.key}`,
      keys: `g ${target.key}`,
      label: target.label,
      group: "Navigation",
      run: () => router.push(target.href),
    })),
    {
      id: "goto-b",
      keys: "g b",
      label: "Go to Board",
      group: "Navigation",
      run: () => router.push(projectHref("board")),
    },
    {
      id: "goto-g",
      keys: "g g",
      label: "Go to Graph",
      group: "Navigation",
      run: () => router.push(projectHref("graph")),
    },
  ];

  useShortcuts(shortcuts);
  return null;
}
