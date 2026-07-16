import type { AccessClaims } from "./jwt";

/**
 * The session the UI reasons about — a projection of the access-token claims.
 * Shared by server (RSC layout seeds it) and client (SessionProvider consumes
 * it); deliberately free of any `next/headers` import so both sides can use it.
 */
export interface Session {
  readonly sub: string;
  readonly org: string;
  readonly roles: readonly string[];
  readonly scope: string;
  readonly kind: "user" | "agent";
  readonly agentId?: string;
  /** Access-token expiry (unix seconds). */
  readonly expiresAt: number;
}

export function sessionFromClaims(claims: AccessClaims): Session {
  return {
    sub: claims.sub,
    org: claims.org,
    roles: claims.roles,
    scope: claims.scope,
    kind: claims.agentId ? "agent" : "user",
    agentId: claims.agentId,
    expiresAt: claims.exp,
  };
}
