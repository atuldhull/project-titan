import { describe, expect, it } from "vitest";

import { permissionGranted, rolesCan } from "@/lib/auth/rbac";

describe("RBAC mirror (TITAN-FRT-008/037)", () => {
  it("owner can do anything, including sensitive resources", () => {
    expect(rolesCan(["owner"], "task:create")).toBe(true);
    expect(rolesCan(["owner"], "audit:read")).toBe(true);
    expect(rolesCan(["owner"], "secret:write")).toBe(true);
  });

  it("viewer can only read, never mutate", () => {
    expect(rolesCan(["viewer"], "task:read")).toBe(true);
    expect(rolesCan(["viewer"], "project:read")).toBe(true);
    expect(rolesCan(["viewer"], "task:create")).toBe(false);
    expect(rolesCan(["viewer"], "run:cancel")).toBe(false);
  });

  it("maintainer can transition tasks but not configure agents", () => {
    expect(rolesCan(["maintainer"], "task:transition")).toBe(true);
    expect(rolesCan(["maintainer"], "agent:configure")).toBe(false);
  });

  it("cross-resource *:read never reaches sensitive audit/secret", () => {
    const perms = new Set(["*:read"]);
    expect(permissionGranted(perms, "task:read")).toBe(true);
    expect(permissionGranted(perms, "audit:read")).toBe(false);
    expect(permissionGranted(perms, "secret:read")).toBe(false);
  });

  it("unknown roles grant nothing; no session grants nothing", () => {
    expect(rolesCan(["developer"], "task:read")).toBe(false);
    expect(rolesCan([], "task:read")).toBe(false);
  });
});
