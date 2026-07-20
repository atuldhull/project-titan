import type { BrowserContext } from "@playwright/test";

/**
 * A signed-in browser context, without a backend.
 *
 * `lib/auth/jwt.ts` DECODES the access token and never verifies it — FastAPI is
 * the authority, and the BFF only reads claims to gate the shell (TITAN-FRT-037).
 * That is what lets this suite mint its own token: a well-formed JWT is all the
 * Next layer needs to render an authenticated shell, and no request ever leaves
 * the browser to be rejected.
 *
 * The consequence to be honest about: this proves the SHELL renders for a given
 * role, not that FastAPI would accept the same token. Real token acceptance is
 * covered by the backend's own auth tests.
 */

const ACCESS_COOKIE = "titan_at";
const REFRESH_COOKIE = "titan_rt";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

export interface SessionOptions {
  readonly sub?: string;
  readonly org?: string;
  readonly roles?: readonly string[];
  /** Seconds from now until `exp`. */
  readonly ttlSeconds?: number;
}

export function mintAccessToken(options: SessionOptions = {}): string {
  const {
    sub = "00000000-0000-4000-8000-0000000000e2",
    org = "titan",
    roles = ["owner"],
    ttlSeconds = 3600,
  } = options;
  const header = b64url({ alg: "RS256", typ: "JWT" });
  const payload = b64url({
    sub,
    org,
    roles,
    scope: "",
    exp: Math.floor(Date.now() / 1000) + ttlSeconds,
  });
  // The signature is never checked on this side; FastAPI would reject it, which
  // is exactly why no request in this suite reaches FastAPI.
  return `${header}.${payload}.e2e-unverified`;
}

export async function signIn(
  context: BrowserContext,
  options: SessionOptions = {},
): Promise<void> {
  const token = mintAccessToken(options);
  await context.addCookies(
    [ACCESS_COOKIE, REFRESH_COOKIE].map((name) => ({
      name,
      value: token,
      domain: "127.0.0.1",
      path: "/",
      httpOnly: true,
      secure: false,
      sameSite: "Lax" as const,
    })),
  );
}
