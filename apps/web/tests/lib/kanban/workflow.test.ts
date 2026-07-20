import { describe, expect, it } from "vitest";

import {
  canMove,
  legalTargets,
  moveIndex,
  permittedTargets,
  resolveMove,
  statusLabel,
  type WorkflowDefinition,
} from "@/lib/kanban/workflow";

/** A faithful slice of the real table (apps/api/.../transitions.py). */
const WORKFLOW: WorkflowDefinition = {
  states: ["backlog", "ready", "assigned", "in_progress", "blocked", "done"],
  transitions: [
    {
      action: "ready",
      from_status: "backlog",
      to_status: "ready",
      required_permission: "task:transition",
      requires_actor_agent: false,
    },
    {
      action: "assign",
      from_status: "ready",
      to_status: "assigned",
      required_permission: "task:assign",
      requires_actor_agent: true,
    },
    {
      action: "block",
      from_status: "ready",
      to_status: "blocked",
      required_permission: "task:transition",
      requires_actor_agent: false,
    },
    {
      action: "start",
      from_status: "assigned",
      to_status: "in_progress",
      required_permission: "task:transition",
      requires_actor_agent: false,
    },
  ],
};

describe("resolveMove", () => {
  const index = moveIndex(WORKFLOW);

  it("resolves a drag to the action that performs it", () => {
    // The whole point: a drop is an ACTION, not a status write.
    expect(resolveMove(index, "backlog", "ready")?.action).toBe("ready");
    expect(resolveMove(index, "ready", "assigned")?.action).toBe("assign");
  });

  it("refuses a pair no single action reaches", () => {
    // backlog -> in_progress needs ready + assign + start. Dragging it must do
    // nothing, not fire a guaranteed 409.
    expect(resolveMove(index, "backlog", "in_progress")).toBeNull();
    expect(resolveMove(index, "done", "backlog")).toBeNull();
  });

  it("refuses a drop on the card's own column", () => {
    expect(resolveMove(index, "ready", "ready")).toBeNull();
  });

  it("declines an ambiguous pair rather than guessing", () => {
    // Two actions from A to B carry no way to pick one. Today's table has no
    // such pair; this is what stops a future one silently firing the wrong one.
    const ambiguous: WorkflowDefinition = {
      states: ["a", "b"],
      transitions: [
        {
          action: "one",
          from_status: "a",
          to_status: "b",
          required_permission: "task:transition",
          requires_actor_agent: false,
        },
        {
          action: "two",
          from_status: "a",
          to_status: "b",
          required_permission: "task:transition",
          requires_actor_agent: false,
        },
      ],
    };
    expect(resolveMove(moveIndex(ambiguous), "a", "b")).toBeNull();
  });

  it("lists a card's legal targets", () => {
    expect(legalTargets(index, "ready").sort()).toEqual([
      "assigned",
      "blocked",
    ]);
    expect(legalTargets(index, "done")).toEqual([]);
  });
});

describe("canMove", () => {
  const index = moveIndex(WORKFLOW);
  // The real bundles: operator holds task:transition but NOT task:assign.
  const operator = (p: string) =>
    p === "task:transition" || p.endsWith(":read");
  const viewer = (p: string) => p.endsWith(":read");
  const admin = () => true;

  it("gates each edge on ITS permission, not one board-wide permission", () => {
    // An operator can move a card along the pipeline but cannot assign one —
    // a board that gated every drag on task:transition would offer that drop
    // and eat a 403.
    expect(canMove(index, operator, "backlog", "ready")).toBe(true);
    expect(canMove(index, operator, "ready", "assigned")).toBe(false);
    expect(canMove(index, admin, "ready", "assigned")).toBe(true);
  });

  it("lets a viewer move nothing", () => {
    expect(canMove(index, viewer, "backlog", "ready")).toBe(false);
  });

  it("is false for an illegal edge no matter the permission", () => {
    expect(canMove(index, admin, "backlog", "done")).toBe(false);
  });
});

describe("permittedTargets", () => {
  const index = moveIndex(WORKFLOW);
  const operator = (p: string) =>
    p === "task:transition" || p.endsWith(":read");
  const admin = () => true;

  it("answers for every from-status, not just the one being dragged", () => {
    // The board needs this with NO drag in progress: §8's "Move to…" menu has
    // to offer a card its targets while the card is sitting still.
    const targets = permittedTargets(index, admin, WORKFLOW.states);
    expect(targets.get("ready")).toEqual(["assigned", "blocked"]);
    expect(targets.get("backlog")).toEqual(["ready"]);
    expect(targets.get("done")).toBeUndefined();
  });

  it("orders targets the way the columns read, not the way the table lists them", () => {
    const targets = permittedTargets(index, admin, [
      "blocked",
      "assigned",
      "ready",
    ]);
    expect(targets.get("ready")).toEqual(["blocked", "assigned"]);
  });

  it("gates each edge on ITS permission", () => {
    // An operator holds task:transition but not task:assign, so a ready card's
    // menu offers Blocked and not Assigned.
    expect(
      permittedTargets(index, operator, WORKFLOW.states).get("ready"),
    ).toEqual(["blocked"]);
  });

  it("keeps a target the states list does not mention", () => {
    // A card can move to a status `states` omits; `boardColumns` renders those
    // as extra columns, so the move must still be offered — appended, not
    // dropped.
    expect(permittedTargets(index, admin, ["assigned"]).get("ready")).toEqual([
      "assigned",
      "blocked",
    ]);
  });

  it("never offers the card's own column, even for a self-edge", () => {
    // The regression this pins down. `permittedTargets` reads the RAW (from,
    // to) index, where `resolveMove`'s "a drop on your own column is not a
    // move" rule does not apply on its own — so a self-edge (the machine is
    // free to grow one; the service already has a `resume` path that writes
    // from_status == to_status) would put a card's own column in its "Move to…"
    // menu as an item that silently no-ops, and light that column up as a legal
    // drop target that then refuses the drop. Fails the moment the `to !== from`
    // filter comes out of either function.
    const withSelfEdge = moveIndex({
      states: ["ready", "blocked"],
      transitions: [
        ...WORKFLOW.transitions,
        {
          action: "resume",
          from_status: "ready",
          to_status: "ready",
          required_permission: "task:transition",
          requires_actor_agent: false,
        },
      ],
    });
    expect(permittedTargets(withSelfEdge, admin, WORKFLOW.states)).toEqual(
      new Map([
        ["backlog", ["ready"]],
        // "ready" itself is absent, and that is the whole assertion.
        ["ready", ["assigned", "blocked"]],
        ["assigned", ["in_progress"]],
      ]),
    );
    expect(legalTargets(withSelfEdge, "ready").sort()).toEqual([
      "assigned",
      "blocked",
    ]);
  });
});

describe("statusLabel", () => {
  it("renders a status as a column heading", () => {
    expect(statusLabel("in_progress")).toBe("In progress");
    expect(statusLabel("backlog")).toBe("Backlog");
  });
});
