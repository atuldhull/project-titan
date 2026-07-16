import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/headers", () => ({
  cookies: async () => ({ get: () => undefined }),
}));

import { proxyToApi } from "@/lib/auth/proxy";

type Handler = (
  url: string,
  headers: Headers,
) => { status: number; body: unknown };

function fakeFetch(handler: Handler): typeof fetch {
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    const headers = new Headers(
      typeof input === "string" ? init?.headers : (input as Request).headers,
    );
    const { status, body } = handler(url, headers);
    return new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;
}

const baseOptions = {
  method: "GET",
  path: "/api/v1/notifications",
  search: "",
  requestHeaders: new Headers({ accept: "application/json" }),
  body: null,
};

beforeEach(() => {
  delete process.env.TITAN_API_ORIGIN;
  delete process.env.NEXT_PUBLIC_API_URL;
});

describe("proxyToApi (BFF pass-through)", () => {
  it("forwards the access bearer and returns the upstream body", async () => {
    let seenAuth = "";
    const outcome = await proxyToApi({
      ...baseOptions,
      access: "access-1",
      fetchImpl: fakeFetch((_url, headers) => {
        seenAuth = headers.get("authorization") ?? "";
        return { status: 200, body: { items: [] } };
      }),
    });
    expect(seenAuth).toBe("Bearer access-1");
    expect(outcome.status).toBe(200);
    expect(outcome.refreshed).toBeNull();
    expect(outcome.unauthenticated).toBe(false);
  });

  it("refreshes once on 401 and retries with the new token", async () => {
    let call = 0;
    const outcome = await proxyToApi({
      ...baseOptions,
      access: "expired",
      refresh: "refresh-1",
      fetchImpl: fakeFetch((url, headers) => {
        if (url.endsWith("/api/v1/auth/refresh")) {
          return {
            status: 200,
            body: {
              access_token: "access-2",
              refresh_token: "refresh-2",
              token_type: "Bearer",
              expires_in: 900,
            },
          };
        }
        call += 1;
        if (call === 1) {
          return { status: 401, body: {} };
        }
        expect(headers.get("authorization")).toBe("Bearer access-2");
        return { status: 200, body: { ok: true } };
      }),
    });
    expect(outcome.status).toBe(200);
    expect(outcome.refreshed?.access_token).toBe("access-2");
  });

  it("single-flights concurrent refreshes so the token family is not revoked", async () => {
    let refreshCalls = 0;
    const fetchImpl = fakeFetch((url) => {
      if (url.endsWith("/api/v1/auth/refresh")) {
        refreshCalls += 1;
        return {
          status: 200,
          body: {
            access_token: "access-2",
            refresh_token: "refresh-2",
            token_type: "Bearer",
            expires_in: 900,
          },
        };
      }
      // Always 401 on the old token; the retry carries the new one.
      return { status: 200, body: { ok: true } };
    });
    // Six parallel requests all hit a 401 at once (a tab waking after the 15-min
    // access TTL). Presenting the same single-use refresh token concurrently
    // would trip the backend's reuse detection and revoke the whole family.
    const outcomes = await Promise.all(
      Array.from({ length: 6 }, () =>
        proxyToApi({
          ...baseOptions,
          access: "expired",
          refresh: "refresh-shared",
          fetchImpl: fakeFetch((url, headers) => {
            if (url.endsWith("/api/v1/auth/refresh")) {
              refreshCalls += 1;
              return {
                status: 200,
                body: {
                  access_token: "access-2",
                  refresh_token: "refresh-2",
                  token_type: "Bearer",
                  expires_in: 900,
                },
              };
            }
            return headers.get("authorization") === "Bearer access-2"
              ? { status: 200, body: { ok: true } }
              : { status: 401, body: {} };
          }),
        }),
      ),
    );
    expect(refreshCalls).toBe(1);
    expect(outcomes.every((o) => o.status === 200)).toBe(true);
    void fetchImpl;
  });

  it("forwards If-Match upstream and ETag back (optimistic concurrency)", async () => {
    let seenIfMatch = "";
    const outcome = await proxyToApi({
      ...baseOptions,
      method: "PATCH",
      requestHeaders: new Headers({
        "content-type": "application/json",
        "if-match": '"v7"',
        "idempotency-key": "abc",
      }),
      access: "access-1",
      fetchImpl: (async (input: RequestInfo | URL, init?: RequestInit) => {
        seenIfMatch = new Headers(init?.headers).get("if-match") ?? "";
        void input;
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "content-type": "application/json", etag: '"v8"' },
        });
      }) as typeof fetch,
    });
    // Without these the API answers 428 forever and the client can never learn
    // the version it must echo.
    expect(seenIfMatch).toBe('"v7"');
    expect(outcome.headers.get("etag")).toBe('"v8"');
  });

  it("reports 502 when FastAPI is unreachable rather than throwing", async () => {
    const outcome = await proxyToApi({
      ...baseOptions,
      access: "access-1",
      fetchImpl: (async () => {
        throw new TypeError("fetch failed");
      }) as typeof fetch,
    });
    expect(outcome.status).toBe(502);
    expect(outcome.upstreamUnreachable).toBe(true);
  });

  it("reports unauthenticated when 401 and no refresh token", async () => {
    const outcome = await proxyToApi({
      ...baseOptions,
      access: "expired",
      fetchImpl: fakeFetch(() => ({ status: 401, body: {} })),
    });
    expect(outcome.status).toBe(401);
    expect(outcome.unauthenticated).toBe(true);
    expect(outcome.refreshed).toBeNull();
  });
});
