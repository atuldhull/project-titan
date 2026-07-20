import { redirect } from "next/navigation";

import { getSession } from "@/lib/auth/session";
import { Providers } from "@/app/providers";
import { Sidebar } from "@/components/shell/sidebar";
import { TopBar } from "@/components/shell/topbar";
import { BottomTabs } from "@/components/shell/bottom-tabs";
import { SkipLink } from "@/components/shell/skip-link";
import { CommandPalette } from "@/components/shell/command-palette";
import { GlobalShortcuts } from "@/components/shell/global-shortcuts";
import { ShortcutsOverlay } from "@/components/shell/shortcuts-overlay";
import { ShortcutProvider } from "@/lib/shortcuts/provider";

/**
 * Auth-gated app shell (Vol 4 §2.3, §11). The session is resolved server-side
 * from the httpOnly cookie and seeded into the client providers; no session →
 * redirect to login (the server is the authority, TITAN-FRT-037). The socket,
 * query cache, and toast host all mount here, inside the gate.
 */
export default async function DashboardLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const session = await getSession();
  if (!session) {
    redirect("/login");
  }
  return (
    <Providers session={session}>
      <ShortcutProvider>
        <SkipLink />
        <div className="flex h-dvh">
          <Sidebar />
          <div className="flex min-w-0 flex-1 flex-col">
            <TopBar />
            {/*
             * `<main>` is the scroll port, not <body> — so the bottom tab bar's
             * clearance has to be padding HERE. On <body> it would be inside a
             * box that never scrolls, and the last row of every screen would
             * stay hidden under the bar.
             */}
            <main
              id="main"
              tabIndex={-1}
              className="flex-1 overflow-y-auto p-4 pb-20 md:p-6 md:pb-6"
            >
              {children}
            </main>
          </div>
        </div>
        <BottomTabs />
        <GlobalShortcuts />
        <CommandPalette />
        <ShortcutsOverlay />
      </ShortcutProvider>
    </Providers>
  );
}
