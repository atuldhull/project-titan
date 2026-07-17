/**
 * The approval gate for destructive deployment actions (TITAN-FRT-032).
 *
 * "Destructive deployment actions SHALL require explicit confirmation (typed
 * env name for prod) and be RBAC-gated. AC: E2E asserts viewer role sees no
 * actions; admin prod-promote requires typed 'prod'."
 *
 * The phase brief asks for this "generically so later environments slot in",
 * which is exactly right, because most of what it gates does not exist yet:
 * there is no environment column, no promote, no rollback and no abort. What
 * DOES exist is the rule — who may act, and what they must type first — and a
 * rule is only trustworthy if it is decided in one pure place rather than
 * re-derived at each button. So the policy lives here and is tested here; the
 * dialog only renders what it is told.
 *
 * **`can` hides, it never reveals.** The session's roles are ORG-WIDE only
 * (`identity/service.py::_org_roles` filters `project_id IS NULL`), while the
 * server unions them with per-project grants. So a user who is org-viewer but
 * project-admin is told "no" here and would be told "yes" by the server. That
 * asymmetry is safe in this direction and only this one: the client may hide a
 * control the server would allow; it must never show one the server refuses.
 * The server is the sole authority either way.
 */

/** The environments the gate knows about. `local` is the only one with a
 *  backend today; the rest are here so the POLICY is written once. */
export const ENVIRONMENTS = ["local", "staging", "prod"] as const;
export type Environment = (typeof ENVIRONMENTS)[number];

export type DeployAction =
  "promote" | "rollback" | "abort" | "teardown" | "redeploy";

export interface GatePolicy {
  /** May this principal perform the action at all? */
  readonly allowed: boolean;
  /** Must they type the environment name to confirm? */
  readonly requiresTypedName: boolean;
  /** The confirmation the dialog restates. */
  readonly scope: string;
  /** Why it is refused, when it is. */
  readonly reason?: string;
}

/**
 * Which permission gates a deploy action.
 *
 * `project:promote` resolves to exactly owner|admin, and it does so without a
 * seed change: owner holds `*`, admin holds `project:*`, and nobody else's
 * bundle reaches it — maintainer's project grant is the explicit
 * `project:write`, and viewer/operator only ever get `*:read`, which cannot
 * match a non-`read` action. It is the same shape as `project:delete`, which
 * the backend's RBAC matrix already pins at owner|admin.
 *
 * The two obvious alternatives are both wrong, and wrong in ways worth naming:
 *
 * * **`project:write`** looks right and is NOT: maintainer holds it explicitly,
 *   so gating on it would let a maintainer promote to prod. (The first draft of
 *   this used it; the test below caught it.)
 * * **`pipeline:advance`** is worse: maintainer holds it AND so does
 *   `operator` — the bundle every AGENT runs as — so any agent could promote.
 */
export const DEPLOY_PERMISSION = "project:promote";

/** Actions that change what is running, and so need confirmation. */
const DESTRUCTIVE: ReadonlySet<DeployAction> = new Set([
  "promote",
  "rollback",
  "abort",
  "teardown",
]);

/**
 * Environments where a typed name is required.
 *
 * §6.9 says "typed environment name for prod promote"; the phase brief widens
 * it to "for prod promote AND for rollback". A rollback of prod is at least as
 * destructive as a promote to it, so the rule is stated per-environment: any
 * destructive action on a protected environment must be typed out.
 */
const PROTECTED: ReadonlySet<Environment> = new Set(["prod"]);

export function evaluateGate(
  action: DeployAction,
  environment: Environment,
  can: (permission: string) => boolean,
): GatePolicy {
  const scope = `${action} · ${environment}`;

  if (!DESTRUCTIVE.has(action)) {
    return { allowed: can(DEPLOY_PERMISSION), requiresTypedName: false, scope };
  }
  if (!can(DEPLOY_PERMISSION)) {
    return {
      allowed: false,
      requiresTypedName: false,
      scope,
      reason: "Deployment actions are limited to owners and admins.",
    };
  }
  return {
    allowed: true,
    requiresTypedName: PROTECTED.has(environment),
    scope,
  };
}

/**
 * Does the typed text confirm the action?
 *
 * Exact, case-sensitive, trimmed only of surrounding whitespace. A
 * case-insensitive match would let "PROD" through from muscle memory, which
 * defeats the point: the typing exists to make the operator read the word.
 */
export function confirmationMatches(
  typed: string,
  environment: Environment,
): boolean {
  return typed.trim() === environment;
}

/**
 * §6.9's 🔒 — shown "while a release awaits CEO-Agent/human approval".
 *
 * Always false, and deliberately so. There is no pending-approval entity for a
 * release: `deployments.status` is CHECK-constrained to
 * pending|running|failed|stopped (an `awaiting_approval` would be rejected by
 * Postgres), and G12's release approval is a BOOLEAN on a submitted artifact —
 * an unsubmitted one is simply absent, not pending. Rendering a lock would be
 * inventing a state the system cannot be in. Kept as a function so the day a
 * real approval lands, one place changes.
 */
export function awaitsApproval(): boolean {
  return false;
}
