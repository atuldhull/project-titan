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
    <div className="flex flex-col gap-6 p-6 lg:flex-row">
      <SettingsNav />
      {/* min-w-0 so a wide table inside a sub-page scrolls in its OWN
          container rather than stretching the flex row and giving the page
          body a horizontal scrollbar (§8: no horizontal body scroll, ever). */}
      <main className="min-w-0 flex-1">{children}</main>
    </div>
  );
}
