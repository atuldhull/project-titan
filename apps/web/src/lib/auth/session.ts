import { cookies } from "next/headers";
import type { NextResponse } from "next/server";

import {
  createTitanClient,
  type TitanClient,
  type TokenPair,
} from "@titan/api-client";

import { decodeAccessClaims, isExpired } from "./jwt";
import { sessionFromClaims, type Session } from "./types";

/**
 * Server-side session handling for the BFF (TITAN-SEC-013, ADR-SEC-01).
 *
 * The access + refresh tokens live ONLY in httpOnly, Secure (in prod),
 * SameSite=Lax cookies — never in localStorage or client JS. RSC and route
 * handlers read them here to talk to FastAPI; the browser reaches FastAPI only
 * through the `/api/titan` proxy, which re-attaches the bearer server-side.
 */

export const ACCESS_COOKIE = "titan_at";
export const REFRESH_COOKIE = "titan_rt";

/** Both cookies persist for the refresh-token lifetime (7 days); the short-lived
 *  access JWT inside is rotated by the proxy on 401. */
const SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60;

export function apiOrigin(): string {
  // `||` not `??`: an empty-string env var (a common compose/env-file artifact)
  // must fall through, or every upstream URL fails to parse.
  return (
    process.env.TITAN_API_ORIGIN ||
    process.env.NEXT_PUBLIC_API_URL ||
    "http://localhost:8000"
  );
}

interface SessionCookieOptions {
  readonly httpOnly: true;
  readonly secure: boolean;
  readonly sameSite: "lax";
  readonly path: "/";
  readonly maxAge: number;
}

export function sessionCookieOptions(): SessionCookieOptions {
  return {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_MAX_AGE_SECONDS,
  };
}

/** Decode the session projection from an access token (no signature check). */
export function decodeSession(accessToken: string | undefined): Session | null {
  if (!accessToken) {
    return null;
  }
  const claims = decodeAccessClaims(accessToken);
  return claims ? sessionFromClaims(claims) : null;
}

// --- request-scoped reads (RSC + route handlers) ---------------------------

export async function getSessionTokens(): Promise<{
  access?: string;
  refresh?: string;
}> {
  const jar = await cookies();
  return {
    access: jar.get(ACCESS_COOKIE)?.value,
    refresh: jar.get(REFRESH_COOKIE)?.value,
  };
}

/**
 * The current session for RSC gating, or null when unauthenticated.
 *
 * An EXPIRED access token is still a session as long as a refresh token can
 * revive it (the proxy rotates on the next 401). With no refresh cookie there is
 * nothing to revive it with, so the session is dead — otherwise the 7-day access
 * cookie would keep rendering the shell over an API that 401s everything.
 */
export async function getSession(): Promise<Session | null> {
  const { access, refresh } = await getSessionTokens();
  if (!access) {
    return null;
  }
  const claims = decodeAccessClaims(access);
  if (!claims) {
    return null;
  }
  if (isExpired(claims, Math.floor(Date.now() / 1000)) && !refresh) {
    return null;
  }
  return sessionFromClaims(claims);
}

/** A typed FastAPI client carrying the caller's bearer (RSC server fetches). */
export async function serverApi(): Promise<TitanClient> {
  const { access } = await getSessionTokens();
  return createTitanClient({
    baseUrl: apiOrigin(),
    headers: access ? { Authorization: `Bearer ${access}` } : {},
  });
}

// --- cookie writes (route handlers set these on the response) --------------

export function writeSessionCookies(
  response: NextResponse,
  tokens: TokenPair,
): void {
  const options = sessionCookieOptions();
  response.cookies.set(ACCESS_COOKIE, tokens.access_token, options);
  if (tokens.refresh_token) {
    response.cookies.set(REFRESH_COOKIE, tokens.refresh_token, options);
  }
}

export function clearSessionCookies(response: NextResponse): void {
  const expired = { ...sessionCookieOptions(), maxAge: 0 };
  response.cookies.set(ACCESS_COOKIE, "", expired);
  response.cookies.set(REFRESH_COOKIE, "", expired);
}

// --- token refresh (single-use rotation) -----------------------------------

/**
 * In-flight refresh, keyed by the refresh token being spent.
 *
 * SINGLE-FLIGHT IS LOAD-BEARING, not an optimization. Refresh tokens are
 * single-use with family reuse detection: if two requests present the same token
 * concurrently, the backend classifies the loser as a REUSE and revokes the
 * whole family (access tokens included, via the `fam` claim) — hard-logging the
 * user out. A tab idle past the 15-minute access TTL then wakes several parallel
 * queries plus the WS ticket at once, so without this every such tab would be
 * logged out. Collapsing them onto one exchange makes that the normal path.
 *
 * Scope: one Next.js server process. Multiple replicas would still race, which
 * needs a shared lock (or a backend grace window) — noted, not solved here.
 */
let inFlightRefresh: {
  token: string;
  promise: Promise<TokenPair | null>;
} | null = null;

/**
 * Exchange a refresh token for a fresh pair at FastAPI `/auth/refresh`. Returns
 * null on any failure (reuse detection, expiry, network) so the caller can force
 * a re-login. Goes through the typed client, so the actual network call stays
 * inside `@titan/api-client` (TITAN-FRT-014); `fetchImpl` is injectable for
 * tests without this module ever referencing the `fetch` global.
 */
export async function refreshAccessToken(
  refreshToken: string,
  fetchImpl?: typeof fetch,
): Promise<TokenPair | null> {
  if (inFlightRefresh && inFlightRefresh.token === refreshToken) {
    return inFlightRefresh.promise;
  }
  const promise = (async (): Promise<TokenPair | null> => {
    const client = createTitanClient({
      baseUrl: apiOrigin(),
      fetch: fetchImpl,
    });
    try {
      const { data } = await client.POST("/api/v1/auth/refresh", {
        body: { refresh_token: refreshToken },
      });
      return data ?? null;
    } catch {
      return null;
    }
  })();
  inFlightRefresh = { token: refreshToken, promise };
  try {
    return await promise;
  } finally {
    if (inFlightRefresh?.token === refreshToken) {
      inFlightRefresh = null;
    }
  }
}
