import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  permissionGranted,
  rolesCan,
  SENSITIVE_RESOURCES,
} from "@/lib/auth/rbac";

/**
 * `rbac.ts` is a hand-written mirror of the backend's RBAC, and Phase 62 proved
 * that a mirror without a parity test simply drifts: `logs` was added to the
 * backend's SENSITIVE_RESOURCES and not here, so `rolesCan(["viewer"],
 * "logs:read")` answered TRUE in the browser while the server 403'd — while
 * every test below stayed green, because they each name `audit`/`secret` by
 * hand and so can only ever check what someone remembered to type twice.
 *
 * Hence: read the Python. Parsing source in a test is ugly, and the alternative
 * is worse — a security-relevant set that agrees with its authority only by
 * memory. If the Python's shape changes this fails loudly, which is the right
 * outcome for a check whose whole job is noticing change.
 */
const RBAC_PY = resolve(__dirname, "../../../../api/src/titan/gateway/rbac.py");

function backendSensitiveResources(): Set<string> {
  const source = readFileSync(RBAC_PY, "utf8");
  const match = /SENSITIVE_RESOURCES\s*=\s*frozenset\(\{([^}]*)\}\)/.exec(
    source,
  );
  if (!match)
    throw new Error(`could not find SENSITIVE_RESOURCES in ${RBAC_PY}`);
  return new Set(Array.from(match[1].matchAll(/"([^"]+)"/g), (m) => m[1]));
}

describe("the mirror agrees with the backend it mirrors", () => {
  it("has exactly the backend's SENSITIVE_RESOURCES", () => {
    expect([...SENSITIVE_RESOURCES].sort()).toEqual(
      [...backendSensitiveResources()].sort(),
    );
  });

  it("hides every sensitive resource from the *:read wildcard every role holds", () => {
    // Derived from the backend's list, so a resource added there is covered
    // here the moment it lands — which is the whole point.
    for (const resource of backendSensitiveResources()) {
      expect(permissionGranted(new Set(["*:read"]), `${resource}:read`)).toBe(
        false,
      );
      expect(rolesCan(["viewer"], `${resource}:read`)).toBe(false);
      expect(rolesCan(["operator"], `${resource}:read`)).toBe(false);
      expect(rolesCan(["owner"], `${resource}:read`)).toBe(true);
    }
  });

  it("grants admin every sensitive :read the backend seed grants it explicitly", () => {
    // The carve-out has a second half the first parity test missed: once a
    // resource is sensitive, `*:read` no longer reaches it, so admin must carry
    // an EXPLICIT `<res>:read` in the mirror or a control the server allows is
    // hidden. Phase 63 landed exactly that bug — admin lost `logs:read`. Read
    // admin's real seed bundle and require the mirror to match on it.
    const source = readFileSync(
      resolve(__dirname, "../../../../api/scripts/seed.py"),
      "utf8",
    );
    const adminBlock = /"admin":\s*\[([\s\S]*?)\]/.exec(source);
    if (!adminBlock)
      throw new Error("could not find the admin bundle in seed.py");
    const seeded = new Set(
      Array.from(adminBlock[1].matchAll(/"([a-z_]+):read"/g), (m) => m[1]),
    );
    for (const resource of backendSensitiveResources()) {
      if (seeded.has(resource)) {
        expect(rolesCan(["admin"], `${resource}:read`)).toBe(true);
      }
    }
  });
});

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
