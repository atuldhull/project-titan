import type { ReactNode } from "react";

import { SettingsNav } from "@/components/settings/settings-nav";

/**
 * The Settings shell (Vol 4 §6.13).
 *
 * A layout, not a page, so each of the seven sub-pages is its own route and
 * loads independently — §6.13's "each sub-page loads independently (RSC-
 * rendered, minimal JS)". Only the nav is a client component; a sub-page that
 * needs no interactivity ships none.
 */
export default function SettingsLayout({
  children,
}: {
  readonly children: ReactNode;
}) {
  return (
    // `p-6` on top of the shell's own padding double-pads; the shell already
    // pads <main>, and at 375px 48px of it is 13% of the viewport.
    <div className="flex flex-col gap-6 lg:flex-row">
      <SettingsNav />
      {/* A <div>, NOT a <main>: the dashboard shell already renders the page's
          <main> landmark, and nesting a second one gave every /settings/* route
          two — so "skip to main content" and landmark navigation both became
          ambiguous (TITAN-FRT-043).

          min-w-0 so a wide table inside a sub-page scrolls in its OWN
          container rather than stretching the flex row and giving the page
          body a horizontal scrollbar (§8: no horizontal body scroll, ever). */}
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}
