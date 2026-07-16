import { NextResponse, type NextRequest } from "next/server";

import {
  clearSessionCookies,
  getSessionTokens,
  writeSessionCookies,
} from "@/lib/auth/session";
import { proxyToApi } from "@/lib/auth/proxy";
import { PROBLEM_CONTENT_TYPE, problem } from "@/lib/http/problem";

/**
 * The BFF proxy (TITAN-SEC-013). Client-side React Query calls the same-origin
 * `/api/titan/…` and this forwards to FastAPI with the bearer re-attached from
 * the httpOnly cookie, transparently refreshing on 401.
 *
 * The upstream path is taken from the RAW `nextUrl.pathname`, never rebuilt from
 * the decoded `params.path` segments: re-joining decoded text would let a `%3F`
 * inside a segment inject a query string (and silently swallow the real one),
 * and `%2F`/`%2E%2E` would re-route the request. Passing the original bytes
 * through also keeps FastAPI's literal colon-verb routes — e.g.
 * `/notifications/{id}:mark-read` — matching, which per-segment re-encoding
 * would break by escaping the colon.
 */
const PROXY_PREFIX = "/api/titan";
const BODYLESS = new Set(["GET", "HEAD"]);

async function handle(request: NextRequest): Promise<NextResponse> {
  const { access, refresh } = await getSessionTokens();
  if (!access && !refresh) {
    return NextResponse.json(problem(401, "Not authenticated"), {
      status: 401,
      headers: PROBLEM_CONTENT_TYPE,
    });
  }

  const { pathname, search } = request.nextUrl;
  if (!pathname.startsWith(`${PROXY_PREFIX}/`)) {
    return NextResponse.json(problem(404, "Unknown proxy path"), {
      status: 404,
      headers: PROBLEM_CONTENT_TYPE,
    });
  }
  const upstreamPath = pathname.slice(PROXY_PREFIX.length);

  const body = BODYLESS.has(request.method)
    ? null
    : await request.arrayBuffer();

  const outcome = await proxyToApi({
    method: request.method,
    path: upstreamPath,
    search,
    requestHeaders: request.headers,
    body,
    access,
    refresh,
    fetchImpl: fetch,
  });

  const response = new NextResponse(outcome.body, {
    status: outcome.status,
    headers: outcome.headers,
  });
  if (outcome.refreshed) {
    writeSessionCookies(response, outcome.refreshed);
  } else if (outcome.unauthenticated) {
    // Access expired and refresh failed → drop the session so the UI re-logs-in.
    clearSessionCookies(response);
  }
  return response;
}

export {
  handle as GET,
  handle as POST,
  handle as PUT,
  handle as PATCH,
  handle as DELETE,
};
