/**
 * The task DAG's derivations (Vol 4 §6.4).
 */

import { describe, expect, it } from "vitest";

import {
  criticalPath,
  milestoneGroup,
  taskGraphModel,
  type ApiGraph,
} from "@/lib/graph/task-graph";
import { adjacency, connectedChains, reachable } from "@/lib/graph/model";

function api(
  nodes: readonly [string, string][],
  edges: readonly [string, string][],
): ApiGraph {
  return {
    project_id: "p1",
    nodes: nodes.map(([id, status]) => ({ id, title: `Task ${id}`, status })),
    // The API's shape: `task_id` DEPENDS ON `depends_on_id`.
    edges: edges.map(([task_id, depends_on_id]) => ({
      task_id,
      depends_on_id,
    })),
  };
}

describe("taskGraphModel", () => {
  it("inverts the API's edge so work flows blocker -> blocked", () => {
    // The API says "a depends_on b"; the graph must draw b -> a. Getting this
    // backwards mirrors every DAG — and matches the backend's own DOT renderer.
    const model = taskGraphModel(
      api(
        [
          ["a", "backlog"],
          ["b", "done"],
        ],
        [["a", "b"]],
      ),
    );
    expect(model.edges).toEqual([{ from: "b", to: "a", blocked: false }]);
  });

  it("marks an edge blocked when its blocker is not done", () => {
    const model = taskGraphModel(
      api(
        [
          ["a", "backlog"],
          ["b", "in_progress"],
        ],
        [["a", "b"]],
      ),
    );
    expect(model.edges[0].blocked).toBe(true);
  });

  it("does NOT treat a cancelled blocker as satisfied", () => {
    // The server's rule is `status <> 'done'` — cancelled blocks forever. If the
    // graph disagreed with the board about this, the two screens would make
    // different claims about the same task.
    const model = taskGraphModel(
      api(
        [
          ["a", "backlog"],
          ["b", "cancelled"],
        ],
        [["a", "b"]],
      ),
    );
    expect(model.edges[0].blocked).toBe(true);
    expect(model.nodes.find((n) => n.id === "a")?.data.blockedBy).toBe(1);
  });

  it("counts unmet blockers per task", () => {
    const model = taskGraphModel(
      api(
        [
          ["a", "backlog"],
          ["b", "done"],
          ["c", "in_progress"],
        ],
        [
          ["a", "b"],
          ["a", "c"],
        ],
      ),
    );
    expect(model.nodes.find((n) => n.id === "a")?.data.blockedBy).toBe(1);
  });

  it("joins the assignee the graph endpoint does not carry", () => {
    // GraphNode is {id, title, status} — no assignee. §6.4 wants an avatar, so
    // the screen joins the task list; the model must accept that join.
    const model = taskGraphModel(api([["a", "backlog"]], []), {
      assignees: new Map([["a", "agent-1"]]),
    });
    expect(model.nodes[0].data.assigneeAgentId).toBe("agent-1");
  });

  it("is empty for a graph that has not loaded", () => {
    expect(taskGraphModel(undefined)).toEqual({ nodes: [], edges: [] });
  });
});

describe("criticalPath", () => {
  it("finds the longest chain of incomplete work", () => {
    //   a -> b -> c -> d   (long)
    //   e -> f             (short)
    const model = taskGraphModel(
      api(
        [
          ["a", "backlog"],
          ["b", "backlog"],
          ["c", "backlog"],
          ["d", "backlog"],
          ["e", "backlog"],
          ["f", "backlog"],
        ],
        [
          ["b", "a"],
          ["c", "b"],
          ["d", "c"],
          ["f", "e"],
        ],
      ),
    );
    expect(criticalPath(model)).toEqual(["a", "b", "c", "d"]);
  });

  it("excludes completed work — a finished task is not remaining work", () => {
    // a(done) -> b -> c : the path is b,c. Counting `a` would report a chain
    // longer than anything left to do.
    const model = taskGraphModel(
      api(
        [
          ["a", "done"],
          ["b", "backlog"],
          ["c", "backlog"],
        ],
        [
          ["b", "a"],
          ["c", "b"],
        ],
      ),
    );
    expect(criticalPath(model)).toEqual(["b", "c"]);
  });

  it("treats merged as complete", () => {
    const model = taskGraphModel(
      api(
        [
          ["a", "merged"],
          ["b", "backlog"],
        ],
        [["b", "a"]],
      ),
    );
    expect(criticalPath(model)).toEqual(["b"]);
  });

  it("excludes cancelled work — nobody will ever walk that chain", () => {
    const model = taskGraphModel(
      api(
        [
          ["a", "backlog"],
          ["b", "cancelled"],
          ["c", "backlog"],
        ],
        [
          ["b", "a"],
          ["c", "b"],
        ],
      ),
    );
    // The a->b->c chain is severed at the cancelled node; the longest surviving
    // chain is a single node.
    expect(criticalPath(model)).toHaveLength(1);
  });

  it("is empty when every task is done", () => {
    const model = taskGraphModel(
      api(
        [
          ["a", "done"],
          ["b", "merged"],
        ],
        [],
      ),
    );
    expect(criticalPath(model)).toEqual([]);
  });

  it("is deterministic between equal-length paths", () => {
    // Two chains of 2. A flickering highlight on re-render is worse than an
    // arbitrary-but-stable choice.
    const model = taskGraphModel(
      api(
        [
          ["a", "backlog"],
          ["b", "backlog"],
          ["c", "backlog"],
          ["d", "backlog"],
        ],
        [
          ["b", "a"],
          ["d", "c"],
        ],
      ),
    );
    const first = criticalPath(model);
    expect(criticalPath(model)).toEqual(first);
    expect(first).toHaveLength(2);
  });

  it("terminates on a cycle instead of hanging the tab", () => {
    // The server rejects a cycle-closing dependency, so this should be
    // unreachable — but "should be unreachable" is not a reason to hang.
    const model = taskGraphModel(
      api(
        [
          ["a", "backlog"],
          ["b", "backlog"],
        ],
        [
          ["a", "b"],
          ["b", "a"],
        ],
      ),
    );
    expect(criticalPath(model).length).toBeGreaterThan(0);
  });
});

describe("reachable / connectedChains", () => {
  const model = taskGraphModel(
    api(
      [
        ["a", "backlog"],
        ["b", "backlog"],
        ["c", "backlog"],
        ["x", "backlog"],
      ],
      [
        ["b", "a"],
        ["c", "b"],
      ],
    ),
  );
  const adj = adjacency(model);

  it("walks downstream and upstream", () => {
    expect([...reachable(adj, "a", "downstream")].sort()).toEqual(["b", "c"]);
    expect([...reachable(adj, "c", "upstream")].sort()).toEqual(["a", "b"]);
  });

  it("highlights both chains through a node, including itself", () => {
    expect([...connectedChains(adj, "b")].sort()).toEqual(["a", "b", "c"]);
  });

  it("leaves an unconnected node alone", () => {
    expect([...connectedChains(adj, "x")]).toEqual(["x"]);
  });

  it("drops an edge to a node outside the model", () => {
    // The graph endpoint returns intra-project edges only, but a model built
    // from a filtered subset (a cluster drill-down) can dangle.
    const adj2 = adjacency({
      nodes: [{ id: "a", kind: "task", label: "a", data: {} }],
      edges: [{ from: "a", to: "ghost" }],
    });
    expect(adj2.out.get("a")).toEqual([]);
    expect(adj2.out.has("ghost")).toBe(false);
  });
});

describe("milestoneGroup", () => {
  it("groups by milestone, naming the ungrouped honestly", () => {
    const model = taskGraphModel(
      api(
        [
          ["a", "backlog"],
          ["b", "backlog"],
        ],
        [],
      ),
      {
        milestones: new Map([["a", "m1"]]),
      },
    );
    expect(milestoneGroup(model.nodes[0])).toBe("m1");
    expect(milestoneGroup(model.nodes[1])).toBe("No milestone");
  });
});
