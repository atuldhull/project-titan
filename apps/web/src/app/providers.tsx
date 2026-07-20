"use client";

import { QueryProvider } from "@/lib/query/provider";
import { SessionProvider } from "@/lib/auth/session-context";
import { WsProvider } from "@/lib/ws/manager";
import { ToastHost } from "@/components/notifications/toast-host";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Session } from "@/lib/auth/types";

/**
 * The client provider stack for the auth-gated dashboard. Order matters: the
 * query client wraps everything (the socket and notifications invalidate its
 * caches); the session is seeded by the RSC layout; the socket connects for the
 * tab; the toast host renders bottom-right.
 *
 * `TooltipProvider` is mounted here rather than per-screen because a `Tooltip`
 * with no provider above it THROWS — it is not a degraded tooltip, it is a
 * render crash, and thrown during SSR it takes the entire route down. It used
 * to be mounted only inside the Kanban board, so the first screen outside
 * Kanban to reach for a tooltip broke server rendering on /org, /projects,
 * /logs and three more at once. The board keeps its own nested provider: Radix
 * allows nesting and the inner `delayDuration` there is deliberate.
 */
export function Providers({
  session,
  children,
}: {
  session: Session | null;
  children: React.ReactNode;
}) {
  return (
    <QueryProvider>
      <SessionProvider session={session}>
        <WsProvider>
          <TooltipProvider>
            {children}
            <ToastHost />
          </TooltipProvider>
        </WsProvider>
      </SessionProvider>
    </QueryProvider>
  );
}
