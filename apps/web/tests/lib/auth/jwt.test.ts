import { describe, expect, it } from "vitest";

import { decodeAccessClaims, isExpired } from "@/lib/auth/jwt";

function makeJwt(payload: Record<string, unknown>): string {
  const encode = (obj: unknown) =>
    Buffer.from(JSON.stringify(obj)).toString("base64url");
  return `${encode({ alg: "RS256", typ: "JWT" })}.${encode(payload)}.signature`;
}

describe("access token claims", () => {
  it("decodes sub/org/roles/scope/exp", () => {
    const token = makeJwt({
      sub: "user-1",
      org: "titan",
      roles: ["admin", "viewer"],
      scope: "user",
      exp: 1_800_000_000,
    });
    expect(decodeAccessClaims(token)).toEqual({
      sub: "user-1",
      org: "titan",
      roles: ["admin", "viewer"],
      scope: "user",
      exp: 1_800_000_000,
      agentId: undefined,
    });
  });

  it("returns null for a token missing sub or roles", () => {
    expect(decodeAccessClaims(makeJwt({ org: "titan" }))).toBeNull();
    expect(decodeAccessClaims("not-a-jwt")).toBeNull();
  });

  it("detects expiry with a small skew margin", () => {
    const claims = { sub: "u", org: "titan", roles: [], scope: "", exp: 1000 };
    expect(isExpired(claims, 900)).toBe(false);
    expect(isExpired(claims, 1000)).toBe(true);
    expect(isExpired(claims, 2000)).toBe(true);
  });
});
