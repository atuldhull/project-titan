import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const cookieStore = new Map<string, string>();

vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: (name: string) =>
      cookieStore.has(name)
        ? { name, value: cookieStore.get(name) }
        : undefined,
  }),
}));

import { GET as sessionGet } from "@/app/api/auth/session/route";
import { POST as loginPost } from "@/app/api/auth/login/route";
import { POST as logoutPost } from "@/app/api/auth/logout/route";
import { GET as ticketGet } from "@/app/api/ws-ticket/route";
import { GET as proxyGet } from "@/app/api/titan/[...path]/route";

function makeJwt(payload: Record<string, unknown>): string {
  const encode = (obj: unknown) =>
    Buffer.from(JSON.stringify(obj)).toString("base64url");
  return `${encode({ alg: "RS256" })}.${encode(payload)}.sig`;
}

function validAccess(overrides: Record<string, unknown> = {}): string {
  return makeJwt({
    sub: "user-1",
    org: "titan",
    roles: ["admin"],
    scope: "user",
    exp: Math.floor(Date.now() / 1000) + 600,
    ...overrides,
  });
}

function stubFetch(
  handler: (url: string, init?: RequestInit) => Response,
): void {
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    return Promise.resolve(handler(url, init));
  });
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  cookieStore.clear();
  delete process.env.TITAN_API_ORIGIN;
  delete process.env.NEXT_PUBLIC_API_URL;
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("POST /api/auth/login", () => {
  it("exchanges credentials, sets httpOnly cookies, returns the session", async () => {
    stubFetch((url) => {
      expect(url).toContain("/api/v1/auth/token");
      return json({
        access_token: validAccess(),
        refresh_token: "refresh-1",
        token_type: "Bearer",
        expires_in: 900,
      });
    });
    const request = new NextRequest("http://localhost/api/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email: "admin@titan.local", password: "pw" }),
    });
    const response = await loginPost(request);
    expect(response.status).toBe(200);
    const at = response.cookies.get("titan_at");
    expect(at?.value).toBeTruthy();
    expect(at?.httpOnly).toBe(true);
    expect(response.cookies.get("titan_rt")?.value).toBe("refresh-1");
    const body = (await response.json()) as { session: { sub: string } | null };
    expect(body.session?.sub).toBe("user-1");
  });

  it("rejects a missing password with 400", async () => {
    const request = new NextRequest("http://localhost/api/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email: "a@b.c" }),
    });
    const response = await loginPost(request);
    expect(response.status).toBe(400);
  });

  it("passes an upstream auth failure through", async () => {
    stubFetch(() => json({ title: "Invalid credentials", status: 401 }, 401));
    const request = new NextRequest("http://localhost/api/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email: "a@b.c", password: "wrong" }),
    });
    const response = await loginPost(request);
    expect(response.status).toBe(401);
  });
});

describe("POST /api/auth/logout", () => {
  it("clears the session cookies and returns 204", async () => {
    cookieStore.set("titan_at", validAccess());
    stubFetch(() => new Response(null, { status: 204 }));
    const response = await logoutPost();
    expect(response.status).toBe(204);
    expect(response.cookies.get("titan_at")?.value).toBe("");
    expect(response.cookies.get("titan_rt")?.value).toBe("");
  });
});

describe("GET /api/auth/session", () => {
  it("returns the decoded session when a cookie is present", async () => {
    cookieStore.set("titan_at", validAccess());
    const response = await sessionGet();
    const body = (await response.json()) as {
      session: { roles: string[] } | null;
    };
    expect(body.session?.roles).toEqual(["admin"]);
  });

  it("returns null when unauthenticated", async () => {
    const response = await sessionGet();
    const body = (await response.json()) as { session: unknown };
    expect(body.session).toBeNull();
  });
});

describe("GET /api/ws-ticket", () => {
  it("returns the access token when the session is valid", async () => {
    const token = validAccess();
    cookieStore.set("titan_at", token);
    const response = await ticketGet();
    expect(response.status).toBe(200);
    const body = (await response.json()) as { token: string };
    expect(body.token).toBe(token);
  });

  it("returns 401 when there is no session", async () => {
    const response = await ticketGet();
    expect(response.status).toBe(401);
  });
});

describe("GET /api/titan/[...path] proxy", () => {
  it("returns 401 without a session", async () => {
    const request = new NextRequest("http://localhost/api/titan/api/v1/tasks");
    const response = await proxyGet(request);
    expect(response.status).toBe(401);
  });

  it("forwards to FastAPI with the bearer when authenticated", async () => {
    cookieStore.set("titan_at", validAccess());
    let seenAuth = "";
    stubFetch((url, init) => {
      expect(url).toContain("/api/v1/tasks");
      seenAuth = new Headers(init?.headers).get("authorization") ?? "";
      return json({ items: [] });
    });
    const request = new NextRequest(
      "http://localhost/api/titan/api/v1/tasks?limit=10",
    );
    const response = await proxyGet(request);
    expect(response.status).toBe(200);
    expect(seenAuth).toContain("Bearer ");
  });
});
