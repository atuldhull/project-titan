import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { SessionProvider } from "@/lib/auth/session-context";
import { useCan } from "@/lib/auth/use-can";
import type { Session } from "@/lib/auth/types";

function session(roles: string[]): Session {
  return {
    sub: "u1",
    org: "titan",
    roles,
    scope: "user",
    kind: "user",
    expiresAt: 0,
  };
}

function Probe({ permission }: { permission: string }) {
  return <span>{useCan(permission) ? "allowed" : "denied"}</span>;
}

function renderWith(current: Session | null, permission: string) {
  return render(
    <SessionProvider session={current}>
      <Probe permission={permission} />
    </SessionProvider>,
  );
}

describe("useCan (UI RBAC gating)", () => {
  it("denies a viewer a mutation", () => {
    renderWith(session(["viewer"]), "task:create");
    expect(screen.getByText("denied")).toBeInTheDocument();
  });

  it("allows an admin a mutation", () => {
    renderWith(session(["admin"]), "task:create");
    expect(screen.getByText("allowed")).toBeInTheDocument();
  });

  it("denies when there is no session", () => {
    renderWith(null, "task:read");
    expect(screen.getByText("denied")).toBeInTheDocument();
  });
});
