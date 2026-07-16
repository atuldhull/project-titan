import { NextResponse } from "next/server";

import { createTitanClient } from "@titan/api-client";

import {
  apiOrigin,
  clearSessionCookies,
  getSessionTokens,
} from "@/lib/auth/session";

/**
 * BFF logout. Best-effort revokes the access token's `jti` at FastAPI
 * `/auth/revoke`, then always clears the session cookies so the browser ends up
 * logged out even if the revoke call fails.
 */
export async function POST(): Promise<NextResponse> {
  const { access } = await getSessionTokens();
  if (access) {
    const client = createTitanClient({
      baseUrl: apiOrigin(),
      headers: { Authorization: `Bearer ${access}` },
    });
    try {
      await client.POST("/api/v1/auth/revoke");
    } catch {
      // Revocation is best-effort — the cookie clear below is what logs out.
    }
  }
  const response = new NextResponse(null, { status: 204 });
  clearSessionCookies(response);
  return response;
}
