"use client";

import { createContext, useContext } from "react";

import type { Session } from "./types";

const SessionContext = createContext<Session | null>(null);

/**
 * Provides the RSC-resolved session to client components. Seeded once by the
 * dashboard layout (a server component) so `useCan`/`useSession` never trigger a
 * client fetch on first paint.
 */
export function SessionProvider({
  session,
  children,
}: {
  session: Session | null;
  children: React.ReactNode;
}) {
  return (
    <SessionContext.Provider value={session}>
      {children}
    </SessionContext.Provider>
  );
}

/** The current session, or `null` when unauthenticated. */
export function useSession(): Session | null {
  return useContext(SessionContext);
}
