"use client";

import { QueryProvider } from "@/lib/query/provider";
import { SessionProvider } from "@/lib/auth/session-context";
import { WsProvider } from "@/lib/ws/manager";
import { ToastHost } from "@/components/notifications/toast-host";
import type { Session } from "@/lib/auth/types";

/**
 * The client provider stack for the auth-gated dashboard. Order matters: the
 * query client wraps everything (the socket and notifications invalidate its
 * caches); the session is seeded by the RSC layout; the socket connects for the
 * tab; the toast host renders bottom-right.
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
          {children}
          <ToastHost />
        </WsProvider>
      </SessionProvider>
    </QueryProvider>
  );
}
