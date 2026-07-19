/**
 * Client-side RBAC mirror for UI gating (TITAN-FRT-008, TITAN-FRT-037).
 *
 * The five seeded roles and their permission bundles mirror
 * `apps/api/scripts/seed.py::ROLE_PERMISSIONS`, and the wildcard-matching logic
 * mirrors `gateway/rbac.py::permission_granted`. This exists ONLY to hide or
 * disable controls the user cannot use — it is convenience, never security. The
 * server re-checks every mutation and is the sole authority; a forged request
 * from a viewer still returns 403.
 *
 * Note (Vol 4 brief vs. backend): the brief names roles
 * owner/admin/developer/viewer; the backend actually seeds
 * owner/admin/maintainer/operator/viewer (no `developer` — `operator` is the
 * agent/machine bundle). We follow the backend, since it is the authority.
 */

export const ROLES = [
  "owner",
  "admin",
  "maintainer",
  "operator",
  "viewer",
] as const;
export type Role = (typeof ROLES)[number];

/**
 * Resources a cross-resource `*:action` wildcard does NOT reach.
 *
 * Mirrors `gateway/rbac.py::SENSITIVE_RESOURCES`, and must be kept in step with
 * it: this set is the only thing stopping `*:read` — which viewer, operator,
 * maintainer and admin all carry — from granting these in the UI.
 *
 * Phase 62 added `logs` to the backend carve-out and left this stale, so
 * `rolesCan(["viewer"], "logs:read")` answered TRUE in the browser while the
 * server 403'd. It never surfaced only because that screen deliberately renders
 * the server's real 403 rather than calling `useCan` — i.e. luck, not design.
 * Exported so a test can assert the two sets agree.
 */
export const SENSITIVE_RESOURCES: ReadonlySet<string> = new Set([
  "audit",
  "logs",
  "member",
  "secret",
  "settings",
]);

const ROLE_PERMISSIONS: Record<Role, readonly string[]> = {
  owner: ["*"],
  admin: [
    "project:*",
    "agent:configure",
    "task:*",
    "run:cancel",
    "run:create",
    "message:write",
    "decision:write",
    "audit:read",
    // Explicit, like the backend seed: `logs` is in SENSITIVE_RESOURCES, so
    // the `*:read` below does NOT reach it. Without this line admin would see a
    // logs control hidden that the server would allow.
    "logs:read",
    "tool_approval:write",
    "memory:write",
    "memory:admin",
    "kb:propose",
    "kb:approve",
    "pipeline:advance",
    "pipeline:override",
    "plugin:admin",
    // Same shape and same reason as `logs:read` above (Phase 64): `member` and
    // `settings` are in SENSITIVE_RESOURCES, so `*:read` does not reach them.
    "member:read",
    "member:write",
    "settings:read",
    "settings:write",
    "*:read",
  ],
  maintainer: [
    "project:write",
    "task:create",
    "task:update",
    "task:transition",
    "task:assign",
    "task:cancel",
    "decision:write",
    "message:write",
    "tool_approval:write",
    "kb:propose",
    "kb:approve",
    "pipeline:advance",
    "*:read",
  ],
  operator: [
    "task:transition",
    "run:create",
    "message:write",
    "memory:write",
    "kb:propose",
    "kb:approve",
    "pipeline:advance",
    "workspace:read",
    "workspace:write",
    "terminal:run",
    "docker:run",
    "browser:use",
    "web:search",
    "web:fetch",
    "*:read",
  ],
  viewer: ["*:read"],
};

function isRole(value: string): value is Role {
  return (ROLES as readonly string[]).includes(value);
}

/** Union of the permission bundles for the given role names (unknown ignored). */
export function collectPermissions(roles: readonly string[]): Set<string> {
  const out = new Set<string>();
  for (const role of roles) {
    if (isRole(role)) {
      for (const perm of ROLE_PERMISSIONS[role]) {
        out.add(perm);
      }
    }
  }
  return out;
}

/** Mirror of `permission_granted`: `r:a` needs `*`, `r:a`, `r:*`, or `*:a`. */
export function permissionGranted(
  permissions: ReadonlySet<string>,
  required: string,
): boolean {
  if (permissions.has("*") || permissions.has(required)) {
    return true;
  }
  const [resource, action] = required.split(":");
  if (permissions.has(`${resource}:*`)) {
    return true;
  }
  // Cross-resource `*:action` never reaches audit/secret.
  if (!SENSITIVE_RESOURCES.has(resource) && permissions.has(`*:${action}`)) {
    return true;
  }
  return false;
}

/** Whether the roles carried in a session grant a `resource:action` permission. */
export function rolesCan(roles: readonly string[], required: string): boolean {
  return permissionGranted(collectPermissions(roles), required);
}
