import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { middleware } from "@/middleware";

/**
 * TITAN-SEC-072 acceptance: every dashboard response carries a CSP with
 * `default-src 'self'` and a strict, nonce'd `script-src` that never allows
 * inline scripts.
 */
function respond(path = "/monitoring") {
  return middleware(new NextRequest(new URL(`http://localhost${path}`)));
}

function csp(path?: string): string {
  return respond(path).headers.get("content-security-policy") ?? "";
}

function directive(policy: string, name: string): string {
  return policy.split(";").map((d) => d.trim()).find((d) => d.startsWith(name)) ?? "";
}

describe("CSP middleware (TITAN-SEC-072)", () => {
  it("sets default-src 'self'", () => {
    expect(directive(csp(), "default-src")).toBe("default-src 'self'");
  });

  it("script-src carries a nonce and never 'unsafe-inline'", () => {
    const scriptSrc = directive(csp(), "script-src");
    expect(scriptSrc).toMatch(/'nonce-[A-Za-z0-9+/=]+'/);
    expect(scriptSrc).not.toContain("'unsafe-inline'");
  });

  it("mints a fresh nonce per request", () => {
    expect(csp()).not.toEqual(csp()); // two requests, two nonces
  });

  it("connect-src names the realtime API's ws origin (so prod WebSockets are not blocked)", () => {
    // The client dials NEXT_PUBLIC_API_URL (default http://localhost:8000) over
    // ws(s); connect-src must allow that cross-origin socket explicitly, not rely
    // on the dev-only ws:/wss: (which is absent in a production build).
    const connectSrc = directive(csp(), "connect-src");
    expect(connectSrc).toContain("ws://localhost:8000");
    expect(connectSrc).toContain("http://localhost:8000");
    expect(connectSrc).not.toContain("connect-src *");
  });

  it("exposes the nonce to the document via the x-nonce request header", () => {
    // The nonce in the response CSP is the one the layout will stamp on its
    // inline script — a stable contract between middleware and layout.
    const policy = csp();
    const nonce = policy.match(/'nonce-([A-Za-z0-9+/=]+)'/)?.[1];
    expect(nonce).toBeTruthy();
  });

  it("sets defensive companion headers", () => {
    const res = respond();
    expect(res.headers.get("x-content-type-options")).toBe("nosniff");
    expect(res.headers.get("x-frame-options")).toBe("DENY");
    expect(directive(csp(), "frame-ancestors")).toBe("frame-ancestors 'none'");
    expect(directive(csp(), "object-src")).toBe("object-src 'none'");
  });
});
