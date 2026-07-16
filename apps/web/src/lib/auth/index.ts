/**
 * Client-safe auth surface. Server-only helpers (cookies, refresh, serverApi)
 * live in `./session` and must be imported directly from server code — they are
 * deliberately NOT re-exported here so a client component can import from
 * `@/lib/auth` without pulling in `next/headers`.
 */
export type { Session } from "./types";
export { sessionFromClaims } from "./types";
export { decodeAccessClaims, isExpired, type AccessClaims } from "./jwt";
export {
  ROLES,
  type Role,
  rolesCan,
  permissionGranted,
  collectPermissions,
} from "./rbac";
export { SessionProvider, useSession } from "./session-context";
export { useCan } from "./use-can";
