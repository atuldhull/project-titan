import { describe, expect, it } from "vitest";

import {
  taskDependencies,
  unmetDependencyCounts,
  type Graph,
} from "@/lib/kanban/deps";

function graph(
  nodes: readonly [string, string][],
  edges: readonly [string, string][],
): Graph {
  return {
    project_id: "p1",
    nodes: nodes.map(([id, status]) => ({ id, title: `Task ${id}`, status })),
    edges: edges.map(([task_id, depends_on_id]) => ({
      task_id,
      depends_on_id,
    })),
  };
}

describe("unmetDependencyCounts", () => {
  it("counts blockers that are not done", () => {
    const g = graph(
      [
        ["a", "backlog"],
        ["b", "done"],
        ["c", "in_progress"],
      ],
      [
        ["a", "b"],
        ["a", "c"],
      ],
    );
    expect(unmetDependencyCounts(g).get("a")).toBe(1);
  });

  it("does NOT count a cancelled blocker as satisfied", () => {
    // The server's rule is `status <> 'done'` — cancelled blocks forever. A
    // client that treated it as satisfied would offer a `ready` drag that 409s.
    const g = graph(
      [
        ["a", "backlog"],
        ["b", "cancelled"],
      ],
      [["a", "b"]],
    );
    expect(unmetDependencyCounts(g).get("a")).toBe(1);
  });

  it("counts an edge to an unknown task as unmet, not satisfied", () => {
    const g = graph([["a", "backlog"]], [["a", "missing"]]);
    expect(unmetDependencyCounts(g).get("a")).toBe(1);
  });

  it("leaves an unblocked task out of the map entirely", () => {
    const g = graph(
      [
        ["a", "backlog"],
        ["b", "done"],
      ],
      [["a", "b"]],
    );
    expect(unmetDependencyCounts(g).has("a")).toBe(false);
  });

  it("is empty for a graph that has not loaded", () => {
    expect(unmetDependencyCounts(undefined).size).toBe(0);
  });
});

describe("taskDependencies", () => {
  it("separates what blocks a task from what it blocks", () => {
    const g = graph(
      [
        ["a", "backlog"],
        ["b", "done"],
        ["c", "backlog"],
      ],
      [
        ["a", "b"],
        ["c", "a"],
      ],
    );
    const deps = taskDependencies(g, "a");
    expect(deps.blockedBy.map((d) => [d.node.id, d.satisfied])).toEqual([
      ["b", true],
    ]);
    expect(deps.blocking.map((n) => n.id)).toEqual(["c"]);
  });

  it("is empty for a task with no edges", () => {
    const g = graph([["a", "backlog"]], []);
    expect(taskDependencies(g, "a")).toEqual({ blockedBy: [], blocking: [] });
  });
});
