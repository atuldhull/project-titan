"use client";

import { useSession } from "./session-context";
import { rolesCan } from "./rbac";

/**
 * RBAC guard for the UI (TITAN-FRT-008, TITAN-FRT-037). `useCan("task:create")`
 * returns whether the current session may perform a `resource:action`, so a
 * control can be hidden or disabled. This is convenience only — the server
 * re-checks every mutation and returns 403 on a forged attempt, at which point
 * the mutation layer shows a permission toast.
 */
export function useCan(permission: string): boolean {
  const session = useSession();
  if (!session) {
    return false;
  }
  return rolesCan(session.roles, permission);
}
