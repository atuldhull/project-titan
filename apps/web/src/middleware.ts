import { NextResponse, type NextRequest } from "next/server";

/**
 * TITAN-SEC-072: a Content-Security-Policy on every dashboard document.
 *
 * `default-src 'self'` with a strict `script-src` that carries a PER-REQUEST
 * nonce and NO `'unsafe-inline'`, so LLM-derived content that somehow reached a
 * <script> (it never should — React escapes, markdown is sanitised) still cannot
 * execute. The nonce is generated here, exported to the document via the
 * `x-nonce` request header (the root layout stamps it on the one first-party
 * inline script), and echoed on the REQUEST `content-security-policy` header so
 * Next.js applies the same nonce to its own framework/bootstrap scripts.
 *
 * `style-src` keeps `'unsafe-inline'` — the SEC-072 ban is scoped to scripts, and
 * Next.js + Tailwind emit inline styles; inline styles are not an XSS vector.
 * Dev adds `'unsafe-eval'` / `ws:` for React Fast Refresh only.
 */
export function middleware(request: NextRequest): NextResponse {
  const nonce = generateNonce();
  const isDev = process.env.NODE_ENV !== "production";

  // The realtime client opens a WebSocket to NEXT_PUBLIC_API_URL (a DIFFERENT
  // origin than the web app in the shipped compose/standalone build), so
  // connect-src must name that origin's http + ws(s) forms or the browser blocks
  // the socket and the whole live layer dies in production (review: masked by the
  // dev-only ws:/wss:). Derived from the SAME env the client is built with, so
  // the allowed origin always matches the one the client dials.
  const apiSources = realtimeConnectSources();

  const csp = [
    `default-src 'self'`,
    `script-src 'self' 'nonce-${nonce}'${isDev ? " 'unsafe-eval'" : ""}`,
    `style-src 'self' 'unsafe-inline'`,
    `img-src 'self' data: blob:`,
    `font-src 'self' data:`,
    `connect-src 'self'${apiSources ? ` ${apiSources}` : ""}${isDev ? " ws: wss:" : ""}`,
    `frame-src 'self'`,
    `worker-src 'self' blob:`,
    `object-src 'none'`,
    `base-uri 'self'`,
    `form-action 'self'`,
    `frame-ancestors 'none'`,
  ].join("; ");

  // Forward the nonce + CSP on the REQUEST so the root layout can read the nonce
  // (via next/headers) and Next.js can nonce its own inline scripts.
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("content-security-policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  // The document response the browser enforces, plus defensive companions.
  response.headers.set("content-security-policy", csp);
  response.headers.set("x-content-type-options", "nosniff");
  response.headers.set("referrer-policy", "strict-origin-when-cross-origin");
  response.headers.set("x-frame-options", "DENY");
  return response;
}

function generateNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/**
 * The http + ws(s) origin of the API the realtime client dials, from
 * NEXT_PUBLIC_API_URL (inlined at build, same value the client uses). Returns ""
 * when the API is same-origin (already covered by 'self') or the value is
 * unparseable — never widens the policy to a wildcard.
 */
function realtimeConnectSources(): string {
  const raw = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  try {
    const origin = new URL(raw).origin; // http(s)://host:port
    const ws = origin.replace(/^http/, "ws"); // http->ws, https->wss
    return `${origin} ${ws}`;
  } catch {
    return "";
  }
}

export const config = {
  // Every route EXCEPT the BFF (JSON, own header allowlist) and static assets.
  // A nonce'd CSP only matters for the HTML documents this covers.
  matcher: [
    "/((?!api/|_next/static|_next/image|favicon.ico|manifest.webmanifest).*)",
  ],
};
