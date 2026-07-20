import type { TokenPair } from "@titan/api-client";

import { apiOrigin, refreshAccessToken } from "./session";

/**
 * The BFF pass-through used by the `/api/titan` proxy route (TITAN-SEC-013).
 *
 * A browser React Query call hits the same-origin proxy; this re-attaches the
 * access bearer server-side and forwards to FastAPI. On a 401 (access token
 * expired) it transparently refreshes once and retries, returning the rotated
 * pair so the route handler can update the cookies. The network call is an
 * INJECTED `fetchImpl` — this module never names the `fetch` global, so it needs
 * no FRT-014 exemption and stays unit-testable.
 */

export interface ProxyOutcome {
  readonly status: number;
  /** Allow-listed response headers to replay to the browser (incl. ETag). */
  readonly headers: Headers;
  readonly body: ArrayBuffer;
  /** A rotated token pair when a refresh occurred; the caller re-sets cookies. */
  readonly refreshed: TokenPair | null;
  /** True when the request is unauthenticated even after a refresh attempt. */
  readonly unauthenticated: boolean;
  /** True when FastAPI could not be reached at all (→ 502, not a 401/500 page). */
  readonly upstreamUnreachable: boolean;
}

/**
 * Request headers forwarded upstream. An allowlist — never the browser's Cookie,
 * Host or its own Authorization. `if-match` is load-bearing: the API answers 428
 * Precondition Required on mutating routes without it, so dropping it would make
 * every optimistic-concurrency write permanently fail. `idempotency-key` carries
 * the API's replay protection.
 */
const FORWARDED_REQUEST_HEADERS = [
  "content-type",
  "accept",
  "if-match",
  "if-none-match",
  "idempotency-key",
];

/**
 * Response headers passed back to the browser. `etag` is the other half of the
 * concurrency contract — without it the client can never learn the version it
 * must echo in `if-match`.
 */
export const FORWARDED_RESPONSE_HEADERS = [
  "content-type",
  "etag",
  "location",
  "retry-after",
  "www-authenticate",
  "idempotency-replayed",
  "content-disposition",
];

function upstreamHeaders(requestHeaders: Headers, token?: string): Headers {
  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = requestHeaders.get(name);
    if (value) {
      headers.set(name, value);
    }
  }
  if (token) {
    headers.set("authorization", `Bearer ${token}`);
  }
  // The client's address, APPENDED to any inbound chain (Phase 67,
  // TITAN-SEC-017).
  //
  // Every browser request reaches FastAPI through this proxy, so without this
  // the API sees THIS process's address for every user in the deployment —
  // and per-IP login throttling keyed on that would lock out everyone at once
  // the first time one attacker tried five bad passwords.
  //
  // Appended, never passed through as-is: whatever the browser sent in
  // `x-forwarded-for` is attacker-controlled, so it is preserved for an
  // operator's benefit but our own observation goes last. The API walks the
  // chain right-to-left and only past hops it is configured to trust
  // (`TITAN_TRUSTED_PROXIES`, `gateway/client_ip.py`), so a forged prefix
  // cannot displace the address vouched for here.
  const clientIp = observedClientIp(requestHeaders);
  if (clientIp) {
    const inbound = requestHeaders.get("x-forwarded-for");
    headers.set(
      "x-forwarded-for",
      inbound ? `${inbound}, ${clientIp}` : clientIp,
    );
  }
  return headers;
}

/**
 * This process's own view of who is calling it.
 *
 * Next.js does not expose the socket peer to route handlers, so the platform's
 * forwarding headers are the only source. `x-real-ip` is preferred because it
 * is single-valued: `x-forwarded-for` is a list whose LEFTMOST entry — the one
 * it is tempting to read — is whatever the original client sent, which is to
 * say whatever an attacker typed. The rightmost entry is the one the nearest
 * proxy actually observed.
 *
 * Returns null when nothing fronts this process (a plain `next start` on a
 * laptop). That is honest rather than unfortunate: no header is added, and the
 * API falls back to the socket peer it can see for itself.
 */
function observedClientIp(requestHeaders: Headers): string | null {
  const real = requestHeaders.get("x-real-ip")?.trim();
  if (real) {
    return real;
  }
  const forwarded = requestHeaders.get("x-forwarded-for");
  if (!forwarded) {
    return null;
  }
  const hops = forwarded
    .split(",")
    .map((hop) => hop.trim())
    .filter(Boolean);
  return hops.length > 0 ? (hops[hops.length - 1] ?? null) : null;
}

export async function proxyToApi(options: {
  readonly method: string;
  /** Upstream path, already including the `/api/v1` prefix. */
  readonly path: string;
  /** Raw query string including the leading `?`, or "". */
  readonly search: string;
  readonly requestHeaders: Headers;
  readonly body: ArrayBuffer | null;
  readonly access?: string;
  readonly refresh?: string;
  readonly fetchImpl: typeof fetch;
}): Promise<ProxyOutcome> {
  const { fetchImpl } = options;
  const url = `${apiOrigin()}${options.path}${options.search}`;

  const send = (token?: string): Promise<Response> =>
    fetchImpl(url, {
      method: options.method,
      headers: upstreamHeaders(options.requestHeaders, token),
      body: options.body ?? undefined,
      redirect: "manual",
    });

  let response: Response;
  let refreshed: TokenPair | null = null;
  try {
    response = await send(options.access);

    if (response.status === 401 && options.refresh) {
      const pair = await refreshAccessToken(options.refresh, fetchImpl);
      if (pair) {
        refreshed = pair;
        response = await send(pair.access_token);
      }
    }
  } catch {
    // FastAPI unreachable — report it as a gateway failure rather than letting
    // the throw surface as a 500 HTML page the client would misread as "logged
    // out" / "bad credentials".
    return {
      status: 502,
      headers: new Headers({ "content-type": "application/problem+json" }),
      body: new TextEncoder().encode(
        JSON.stringify({
          type: "about:blank",
          title: "Upstream API unreachable",
          status: 502,
        }),
      ).buffer as ArrayBuffer,
      refreshed: null,
      unauthenticated: false,
      upstreamUnreachable: true,
    };
  }

  const headers = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = response.headers.get(name);
    if (value) {
      headers.set(name, value);
    }
  }

  const body = await response.arrayBuffer();
  return {
    status: response.status,
    headers,
    body,
    refreshed,
    unauthenticated: response.status === 401,
    upstreamUnreachable: false,
  };
}
