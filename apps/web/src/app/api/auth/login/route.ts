import { NextResponse, type NextRequest } from "next/server";

import { createTitanClient } from "@titan/api-client";

import { BFF_REDIRECT_URI, createPkce } from "@/lib/auth/pkce";
import {
  apiOrigin,
  decodeSession,
  writeSessionCookies,
} from "@/lib/auth/session";
import { PROBLEM_CONTENT_TYPE, problem } from "@/lib/http/problem";

/**
 * BFF login (TITAN-SEC-010/013, ADR-SEC-01).
 *
 * The first-party login form. It runs the Authorization Code + PKCE flow
 * server-side — POST `/oauth/authorize` for a one-time code, then POST
 * `/oauth/token` with the verifier — and stores the resulting pair in httpOnly
 * cookies, so tokens never reach client JS and the verifier never leaves this
 * process. The password grant this route used to call is now disabled
 * (SEC-010); see docs/authentication-grants.md.
 *
 * This is NOT a cross-site OIDC callback, so the same-origin CSRF guard below
 * is exactly right and must stay: the credentials arrive from our own login
 * page as a same-origin JSON fetch, and there is no browser redirect to
 * intercept because the BFF drives both legs itself.
 */
/**
 * The client-IP forwarding headers to pass to the API, taken from what fronts
 * this BFF. Only forwards headers a trusted proxy would have set — the API
 * decides whether to believe them (it ignores X-Forwarded-For from an untrusted
 * peer), so this cannot let a browser forge its address. Omitted entirely when
 * absent, so a direct dev run forwards nothing and the API falls back to the
 * socket peer.
 */
function clientIpHeaders(request: NextRequest): Record<string, string> {
  const headers: Record<string, string> = {};
  const forwarded = request.headers.get("x-forwarded-for");
  if (forwarded) {
    headers["x-forwarded-for"] = forwarded;
  }
  const realIp = request.headers.get("x-real-ip");
  if (realIp) {
    headers["x-real-ip"] = realIp;
  }
  return headers;
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  // Login CSRF defence. SameSite=Lax does NOT help here: the attack is a
  // cross-site top-level form POST that logs the victim into the ATTACKER's
  // account. A cross-site form can only send simple content types, and
  // fetch/XHR cannot forge these headers — so requiring JSON + a same-origin
  // fetch blocks it.
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
  const totpCode = payload?.totp_code;
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
  const { verifier, challenge } = await createPkce();

  // Leg 1: authenticate at the authorization endpoint → one-time code.
  //
  // Forward the client's address so the API's per-IP login throttle
  // (TITAN-SEC-017) keys on the real client, not on this BFF process. The
  // throttle runs ONLY at /oauth/authorize, and the API sees the BFF as its
  // peer for every browser — so without this, five bad logins from one attacker
  // would lock out EVERY user (the exact global-lockout the client-IP module
  // exists to prevent). Per-IP throttling therefore REQUIRES a trusted proxy in
  // front of the BFF that sets X-Forwarded-For, with both that proxy and the
  // BFF listed in TITAN_TRUSTED_PROXIES; the API ignores the header from any
  // untrusted peer, so a browser cannot forge it.
  let authorize;
  try {
    authorize = await client.POST("/api/v1/oauth/authorize", {
      body: {
        email,
        password,
        ...(typeof totpCode === "string" && totpCode
          ? { totp_code: totpCode }
          : {}),
        response_type: "code",
        scope: "user",
        redirect_uri: BFF_REDIRECT_URI,
        code_challenge: challenge,
        code_challenge_method: "S256",
      },
      headers: clientIpHeaders(request),
    });
  } catch {
    return NextResponse.json(problem(502, "Upstream API unreachable"), {
      status: 502,
      headers: PROBLEM_CONTENT_TYPE,
    });
  }

  if (!authorize.data) {
    // Pass the upstream status through: a 401 (bad password, or MFA required —
    // the problem `type` distinguishes them so the client can prompt for a
    // second factor rather than re-prompting for the password), a 429
    // (throttled, with Retry-After preserved), or a 400.
    const upstream = authorize.response?.status ?? 502;
    const status = upstream >= 400 ? upstream : 502;
    const headers = new Headers(PROBLEM_CONTENT_TYPE);
    const retryAfter = authorize.response?.headers.get("retry-after");
    if (retryAfter) {
      headers.set("retry-after", retryAfter);
    }
    return NextResponse.json(
      authorize.error ?? problem(status, "Login failed"),
      { status, headers },
    );
  }

  // Leg 2: redeem the code for TITAN's own tokens.
  let token;
  try {
    token = await client.POST("/api/v1/oauth/token", {
      body: {
        grant_type: "authorization_code",
        code: authorize.data.code,
        code_verifier: verifier,
        redirect_uri: BFF_REDIRECT_URI,
      },
    });
  } catch {
    return NextResponse.json(problem(502, "Upstream API unreachable"), {
      status: 502,
      headers: PROBLEM_CONTENT_TYPE,
    });
  }

  if (!token.data) {
    const upstream = token.response?.status ?? 502;
    const status = upstream >= 400 ? upstream : 502;
    return NextResponse.json(token.error ?? problem(status, "Login failed"), {
      status,
      headers: PROBLEM_CONTENT_TYPE,
    });
  }

  const result = NextResponse.json({
    session: decodeSession(token.data.access_token),
  });
  writeSessionCookies(result, token.data);
  return result;
}
