/**
 * TITAN-FRT-032's gate.
 *
 * The AC is "E2E asserts viewer role sees no actions; admin prod-promote
 * requires typed 'prod'". There is no Playwright here, so the E2E is not run —
 * but the RULE it would assert is pure and is asserted exactly, which is the
 * part that can actually be wrong.
 */
import { describe, expect, it } from "vitest";

import {
  awaitsApproval,
  confirmationMatches,
  DEPLOY_PERMISSION,
  evaluateGate,
} from "@/lib/deployments/gate";
import { rolesCan } from "@/lib/auth/rbac";

const canFor = (role: string) => (permission: string) =>
  rolesCan([role], permission);

describe("who may act (FRT-032: RBAC owner|admin)", () => {
  it("lets owner and admin act", () => {
    for (const role of ["owner", "admin"]) {
      expect(evaluateGate("promote", "prod", canFor(role)).allowed).toBe(true);
    }
  });

  it("shows a viewer no actions at all", () => {
    // The AC's first clause, stated against the real role bundles.
    const gate = evaluateGate("promote", "prod", canFor("viewer"));
    expect(gate.allowed).toBe(false);
    expect(gate.reason).toBeTruthy();
  });

  it("refuses maintainer and operator", () => {
    // `operator` is the bundle every AGENT runs as. If the gate were on
    // `pipeline:advance` — which both hold — any agent could promote to prod.
    expect(evaluateGate("rollback", "prod", canFor("maintainer")).allowed).toBe(
      false,
    );
    expect(evaluateGate("rollback", "prod", canFor("operator")).allowed).toBe(
      false,
    );
  });

  it("is gated on a permission that really does mean owner|admin", () => {
    // Pins the choice of permission, not just the gate's plumbing: if
    // `project:write` ever widened, FRT-032 would silently widen with it.
    expect(rolesCan(["owner"], DEPLOY_PERMISSION)).toBe(true);
    expect(rolesCan(["admin"], DEPLOY_PERMISSION)).toBe(true);
    expect(rolesCan(["maintainer"], DEPLOY_PERMISSION)).toBe(false);
    expect(rolesCan(["operator"], DEPLOY_PERMISSION)).toBe(false);
    expect(rolesCan(["viewer"], DEPLOY_PERMISSION)).toBe(false);
  });
});

describe("what must be typed (FRT-032: typed env name for prod)", () => {
  it("requires the typed name for a destructive action on prod", () => {
    expect(
      evaluateGate("promote", "prod", canFor("admin")).requiresTypedName,
    ).toBe(true);
    // The brief widens §6.9's "prod promote" to rollback too — a rollback of
    // prod is at least as destructive as a promote to it.
    expect(
      evaluateGate("rollback", "prod", canFor("admin")).requiresTypedName,
    ).toBe(true);
    expect(
      evaluateGate("abort", "prod", canFor("admin")).requiresTypedName,
    ).toBe(true);
  });

  it("does not require it for an unprotected environment", () => {
    expect(
      evaluateGate("promote", "local", canFor("admin")).requiresTypedName,
    ).toBe(false);
    expect(
      evaluateGate("teardown", "staging", canFor("admin")).requiresTypedName,
    ).toBe(false);
  });

  it("does not require it for a non-destructive action", () => {
    expect(
      evaluateGate("redeploy", "prod", canFor("admin")).requiresTypedName,
    ).toBe(false);
  });

  it("restates the scope for the dialog", () => {
    expect(evaluateGate("promote", "prod", canFor("admin")).scope).toBe(
      "promote · prod",
    );
  });
});

describe("confirmationMatches", () => {
  it("accepts the exact name", () => {
    expect(confirmationMatches("prod", "prod")).toBe(true);
    expect(confirmationMatches("  prod  ", "prod")).toBe(true);
  });

  it("is case-sensitive", () => {
    // The typing exists to make the operator READ the word. Accepting "PROD"
    // from muscle memory defeats the whole control.
    expect(confirmationMatches("PROD", "prod")).toBe(false);
    expect(confirmationMatches("Prod", "prod")).toBe(false);
  });

  it("rejects a near miss and an empty box", () => {
    expect(confirmationMatches("production", "prod")).toBe(false);
    expect(confirmationMatches("", "prod")).toBe(false);
    expect(confirmationMatches("staging", "prod")).toBe(false);
  });
});

describe("awaitsApproval", () => {
  it("is always false, because no release can be in that state", () => {
    // `deployments.status` is CHECK-constrained to
    // pending|running|failed|stopped, so an `awaiting_approval` is rejected by
    // Postgres; G12's release approval is a BOOLEAN on a submitted artifact,
    // and an unsubmitted one is absent, not pending. A lock icon would be
    // inventing a state the system cannot hold.
    expect(awaitsApproval()).toBe(false);
  });
});
