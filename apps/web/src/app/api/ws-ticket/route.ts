import { NextResponse } from "next/server";

import { decodeAccessClaims, isExpired } from "@/lib/auth/jwt";
import {
  getSessionTokens,
  refreshAccessToken,
  writeSessionCookies,
} from "@/lib/auth/session";
import { PROBLEM_CONTENT_TYPE, problem } from "@/lib/http/problem";

/**
 * Issues the short-lived bearer the browser needs to open `/ws?token=…`
 * (TITAN-FRT-018). The realtime endpoint (Phase 24) only authenticates via the
 * query param, so — unlike REST — the socket handshake unavoidably needs the
 * token in JS. We keep the exposure minimal: the ticket is the current access
 * JWT (≤ 15 min), refreshed here first if it has expired, and the REST tokens
 * stay httpOnly. The client re-requests a ticket on every (re)connect.
 */
export async function GET(): Promise<NextResponse> {
  const { access, refresh } = await getSessionTokens();
  const now = Math.floor(Date.now() / 1000);
  const claims = access ? decodeAccessClaims(access) : null;

  if ((!claims || isExpired(claims, now)) && refresh) {
    const pair = await refreshAccessToken(refresh);
    if (pair) {
      const refreshedClaims = decodeAccessClaims(pair.access_token);
      const result = NextResponse.json({
        token: pair.access_token,
        expiresAt: refreshedClaims?.exp ?? null,
      });
      writeSessionCookies(result, pair);
      return result;
    }
  }

  // Also reject an EXPIRED token whose refresh failed or was absent: handing it
  // out would 401 at the WS handshake, and the client would loop (reconnect →
  // ticket → reject), hammering the refresh endpoint.
  if (!access || !claims || isExpired(claims, now)) {
    return NextResponse.json(problem(401, "Not authenticated"), {
      status: 401,
      headers: PROBLEM_CONTENT_TYPE,
    });
  }

  return NextResponse.json({ token: access, expiresAt: claims.exp });
}
