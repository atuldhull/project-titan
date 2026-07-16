import { NextResponse, type NextRequest } from "next/server";

import { createTitanClient } from "@titan/api-client";

import {
  apiOrigin,
  decodeSession,
  writeSessionCookies,
} from "@/lib/auth/session";
import { PROBLEM_CONTENT_TYPE, problem } from "@/lib/http/problem";

/**
 * BFF login (TITAN-SEC-013, ADR-SEC-01). Exchanges an email/password for a token
 * pair at FastAPI `/auth/token`, then stores the pair in httpOnly cookies — the
 * tokens never reach client JS. Shaped so swapping the password grant for an
 * OIDC authorization-code callback later is a route change, not a rework.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  // Login CSRF defence. SameSite=Lax does NOT help here: the attack is a
  // cross-site top-level form POST that logs the victim into the ATTACKER's
  // account (their later work then lands in the attacker's org). A cross-site
  // form can only send simple content types, and fetch/XHR cannot forge these
  // headers — so requiring JSON + a same-origin fetch blocks it.
  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    return NextResponse.json(
      problem(415, "Unsupported media type", "Expected application/json"),
      { status: 415, headers: PROBLEM_CONTENT_TYPE },
    );
  }
  const site = request.headers.get("sec-fetch-site");
  if (site && site !== "same-origin" && site !== "none") {
    return NextResponse.json(problem(403, "Cross-site login is not allowed"), {
      status: 403,
      headers: PROBLEM_CONTENT_TYPE,
    });
  }

  let payload: Record<string, unknown> | null = null;
  try {
    payload = (await request.json()) as Record<string, unknown>;
  } catch {
    payload = null;
  }
  const email = payload?.email;
  const password = payload?.password;
  if (
    typeof email !== "string" ||
    typeof password !== "string" ||
    !email ||
    !password
  ) {
    return NextResponse.json(
      problem(
        400,
        "Invalid credentials payload",
        "email and password are required",
      ),
      { status: 400, headers: PROBLEM_CONTENT_TYPE },
    );
  }

  const client = createTitanClient({ baseUrl: apiOrigin() });
  let data, error, response;
  try {
    ({ data, error, response } = await client.POST("/api/v1/auth/token", {
      body: { grant_type: "password", email, password },
    }));
  } catch {
    // FastAPI unreachable — say so, rather than letting the throw become a 500
    // HTML page the client would render as "invalid credentials".
    return NextResponse.json(problem(502, "Upstream API unreachable"), {
      status: 502,
      headers: PROBLEM_CONTENT_TYPE,
    });
  }

  if (!data) {
    // Clamp to an error status: a 2xx with an unusable body must not reach the
    // client as `ok`, or a failed login would render as a successful one.
    const upstream = response?.status ?? 502;
    const status = upstream >= 400 ? upstream : 502;
    return NextResponse.json(error ?? problem(status, "Login failed"), {
      status,
      headers: PROBLEM_CONTENT_TYPE,
    });
  }

  const result = NextResponse.json({
    session: decodeSession(data.access_token),
  });
  writeSessionCookies(result, data);
  return result;
}
