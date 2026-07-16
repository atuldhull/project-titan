import { decodeJwt } from "jose";

/**
 * Claims carried in a TITAN access token (identity.tokens, RS256).
 *
 * We DECODE (never verify) here: the token lives only in an httpOnly cookie our
 * own BFF set from a FastAPI response, and every proxied request is re-verified
 * by FastAPI (RS256) — so these claims drive UI gating only, and the server is
 * always the authority (TITAN-FRT-037). A tampered cookie fails at FastAPI with
 * a 401, which the proxy turns into a forced re-login.
 */
export interface AccessClaims {
  readonly sub: string;
  readonly org: string;
  readonly roles: readonly string[];
  readonly scope: string;
  /** Expiry (unix seconds). */
  readonly exp: number;
  readonly agentId?: string;
}

export function decodeAccessClaims(token: string): AccessClaims | null {
  let raw: Record<string, unknown>;
  try {
    raw = decodeJwt(token);
  } catch {
    return null;
  }
  if (typeof raw.sub !== "string" || !Array.isArray(raw.roles)) {
    return null;
  }
  const roles = raw.roles.filter((r): r is string => typeof r === "string");
  return {
    sub: raw.sub,
    org: typeof raw.org === "string" ? raw.org : "",
    roles,
    scope: typeof raw.scope === "string" ? raw.scope : "",
    exp: typeof raw.exp === "number" ? raw.exp : 0,
    agentId: typeof raw.agent_id === "string" ? raw.agent_id : undefined,
  };
}

/**
 * True once the token's `exp` has passed (with a small clock-skew margin). A
 * missing/unparseable `exp` decodes to 0 and is treated as EXPIRED — failing
 * closed, since a token that never expires is the worse assumption.
 */
export function isExpired(claims: AccessClaims, nowSeconds: number): boolean {
  if (claims.exp <= 0) {
    return true;
  }
  return claims.exp <= nowSeconds + 5;
}
