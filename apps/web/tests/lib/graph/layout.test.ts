/**
 * Layout, the clustering guard, and the one part of TITAN-FRT-026 this repo can
 * honestly measure.
 *
 * §6.4's acceptance is "300 nodes / 600 edges at ≥ 30 fps pan/zoom, perf test in
 * CI". A frame rate needs a browser compositing real DOM; there is no Playwright
 * or headless-browser harness in this repo, so **fps is NOT measured here and is
 * NOT claimed anywhere**. What is measured is dagre — the expensive, pure step
 * that runs before a single node is painted, and the only part of that budget
 * that lives in JS we control. A layout that took seconds would make ≥30 fps
 * impossible; a fast layout does not prove it. This asserts the necessary half.
 */

import { describe, expect, it } from "vitest";

import {
  CLUSTER_THRESHOLD,
  cluster,
  incrementalLayout,
  layout,
  needsClustering,
} from "@/lib/graph/layout";
import type { GraphModel, Positioned } from "@/lib/graph/model";

interface Data {
  readonly group: string;
}

/** The §6.4 fixture: 300 nodes, 600 edges, in a realistic layered shape. */
function fixture(nodes: number, edges: number): GraphModel<Data> {
  const model: {
    nodes: GraphModel<Data>["nodes"];
    edges: GraphModel<Data>["edges"];
  } = {
    nodes: Array.from({ length: nodes }, (_, i) => ({
      id: `n${i}`,
      kind: "task",
      label: `Task ${i}`,
      data: { group: `m${i % 10}` },
    })),
    edges: [],
  };
  const list: { from: string; to: string }[] = [];
  for (let i = 0; i < edges; i += 1) {
    // Forward-only, so the fixture is a DAG like the real one.
    const from = i % (nodes - 1);
    const to = Math.min(nodes - 1, from + 1 + (i % 7));
    if (from !== to) {
      list.push({ from: `n${from}`, to: `n${to}` });
    }
  }
  return { nodes: model.nodes, edges: list };
}

describe("layout", () => {
  it("positions every node", () => {
    const model = fixture(20, 25);
    const laid = layout(model);
    expect(laid.nodes).toHaveLength(20);
    expect(
      laid.nodes.every((n) => Number.isFinite(n.x) && Number.isFinite(n.y)),
    ).toBe(true);
  });

  it("ranks a chain left-to-right in dependency order", () => {
    const model: GraphModel<Data> = {
      nodes: ["a", "b", "c"].map((id) => ({
        id,
        kind: "task",
        label: id,
        data: { group: "m" },
      })),
      edges: [
        { from: "a", to: "b" },
        { from: "b", to: "c" },
      ],
    };
    const at = new Map(
      layout(model, { direction: "LR" }).nodes.map((n) => [n.id, n]),
    );
    expect(at.get("a")!.x).toBeLessThan(at.get("b")!.x);
    expect(at.get("b")!.x).toBeLessThan(at.get("c")!.x);
  });

  it("survives an edge naming a node that is not in the model", () => {
    // dagre INVENTS a node for an unknown endpoint — that would surface as a
    // phantom position with nothing to render.
    const model: GraphModel<Data> = {
      nodes: [{ id: "a", kind: "task", label: "a", data: { group: "m" } }],
      edges: [{ from: "a", to: "ghost" }],
    };
    const laid = layout(model);
    expect(laid.nodes).toHaveLength(1);
  });

  it("lays out the 300-node / 600-edge fixture fast enough not to be the problem", () => {
    // NOT an fps assertion — see this file's header. The bound is deliberately
    // loose: it is a smoke alarm for an accidentally quadratic change, not a
    // benchmark, and a tight bound would just flake on a busy CI box.
    const model = fixture(300, 600);
    const started = performance.now();
    const laid = layout(model);
    const elapsed = performance.now() - started;
    expect(laid.nodes).toHaveLength(300);
    expect(elapsed).toBeLessThan(2000);
  });
});

describe("incrementalLayout (§6.4: no disorienting jumps)", () => {
  const base: GraphModel<Data> = {
    nodes: ["a", "b"].map((id) => ({
      id,
      kind: "task",
      label: id,
      data: { group: "m" },
    })),
    edges: [{ from: "a", to: "b" }],
  };

  it("holds every existing node EXACTLY still when nothing is new", () => {
    // A status change recolours in place. Re-ranking the graph under a reader is
    // the disorientation §6.4 forbids.
    const first = layout(base);
    const again = incrementalLayout(base, first.nodes);
    for (const node of again.nodes) {
      const was = first.nodes.find((n) => n.id === node.id)!;
      expect({ x: node.x, y: node.y }).toEqual({ x: was.x, y: was.y });
    }
  });

  it("holds existing nodes still while placing a new one", () => {
    const first = layout(base);
    const grown: GraphModel<Data> = {
      nodes: [
        ...base.nodes,
        { id: "c", kind: "task", label: "c", data: { group: "m" } },
      ],
      edges: [...base.edges, { from: "b", to: "c" }],
    };
    const next = incrementalLayout(grown, first.nodes);
    for (const id of ["a", "b"]) {
      const was = first.nodes.find((n) => n.id === id)!;
      const now = next.nodes.find((n) => n.id === id)!;
      expect({ x: now.x, y: now.y }).toEqual({ x: was.x, y: was.y });
    }
    const c = next.nodes.find((n) => n.id === "c")!;
    const b = next.nodes.find((n) => n.id === "b")!;
    expect(c.x).toBeGreaterThan(b.x); // placed beside the neighbour that introduced it
  });

  it("never places a new node ON TOP of an existing one", () => {
    // The commonest DAG shape there is: two tasks depending on one blocker. The
    // natural placement (anchor.x + width + ranksep) is EXACTLY where dagre put
    // the anchor's existing successor, so without a free-slot search the new
    // sibling lands pixel-perfect on top of it — invisible and unclickable, with
    // Re-layout the only escape.
    const first = layout(base); // a -> b
    const fanOut: GraphModel<Data> = {
      nodes: [
        ...base.nodes,
        { id: "c", kind: "task", label: "c", data: { group: "m" } },
      ],
      edges: [...base.edges, { from: "a", to: "c" }], // c ALSO depends on a
    };
    const next = incrementalLayout(fanOut, first.nodes);
    const at = (id: string) => next.nodes.find((n) => n.id === id)!;
    expect({ x: at("c").x, y: at("c").y }).not.toEqual({
      x: at("b").x,
      y: at("b").y,
    });

    // And no PAIR of nodes overlaps, which is the property that actually matters.
    for (const a of next.nodes) {
      for (const b of next.nodes) {
        if (a.id >= b.id) {
          continue;
        }
        const overlaps =
          Math.abs(a.x - b.x) < a.width && Math.abs(a.y - b.y) < a.height;
        expect(overlaps).toBe(false);
      }
    }
  });

  it("does not stack an unanchored new node on the origin", () => {
    const first = layout(base);
    const grown: GraphModel<Data> = {
      nodes: [
        ...base.nodes,
        { id: "lonely", kind: "task", label: "l", data: { group: "m" } },
      ],
      edges: base.edges,
    };
    const next = incrementalLayout(grown, first.nodes);
    const lonely = next.nodes.find((n) => n.id === "lonely")!;
    const maxY = Math.max(...first.nodes.map((n) => n.y));
    expect(lonely.y).toBeGreaterThan(maxY);
  });

  it("places a node it has never seen rather than dropping it", () => {
    const next = incrementalLayout(base, [] as Positioned<Data>[]);
    expect(next.nodes).toHaveLength(2);
  });
});

describe("needsClustering / cluster (ADR-FRT-04's guard)", () => {
  it("trips only past the threshold", () => {
    expect(needsClustering(fixture(CLUSTER_THRESHOLD, 0))).toBe(false);
    expect(needsClustering(fixture(CLUSTER_THRESHOLD + 1, 0))).toBe(true);
  });

  it("collapses to one node per group, carrying the hidden count", () => {
    const model = fixture(30, 0); // groups m0..m9, 3 each
    const clustered = cluster(model, (n) => n.data.group);
    expect(clustered.nodes).toHaveLength(10);
    expect(clustered.nodes.every((n) => n.data.size === 3)).toBe(true);
    expect(clustered.nodes.every((n) => n.kind === "cluster")).toBe(true);
  });

  it("keeps crossing edges and dissolves internal ones", () => {
    const model: GraphModel<Data> = {
      nodes: [
        { id: "a1", kind: "task", label: "a1", data: { group: "A" } },
        { id: "a2", kind: "task", label: "a2", data: { group: "A" } },
        { id: "b1", kind: "task", label: "b1", data: { group: "B" } },
      ],
      edges: [
        { from: "a1", to: "a2" }, // internal to A — vanishes
        { from: "a2", to: "b1" }, // crosses — survives as A -> B
      ],
    };
    const clustered = cluster(model, (n) => n.data.group);
    expect(clustered.edges).toEqual([
      { from: "A", to: "B", blocked: undefined },
    ]);
  });

  it("deduplicates parallel crossing edges, keeping blocked if any member is", () => {
    const model: GraphModel<Data> = {
      nodes: [
        { id: "a1", kind: "task", label: "a1", data: { group: "A" } },
        { id: "a2", kind: "task", label: "a2", data: { group: "A" } },
        { id: "b1", kind: "task", label: "b1", data: { group: "B" } },
      ],
      edges: [
        { from: "a1", to: "b1", blocked: false },
        { from: "a2", to: "b1", blocked: true },
      ],
    };
    const clustered = cluster(model, (n) => n.data.group);
    expect(clustered.edges).toHaveLength(1);
    expect(clustered.edges[0].blocked).toBe(true);
  });

  it("lays out the clustered 600-node graph, which is the point of clustering", () => {
    const model = fixture(600, 900);
    expect(needsClustering(model)).toBe(true);
    const clustered = cluster(model, (n) => n.data.group);
    expect(clustered.nodes).toHaveLength(10);
    expect(layout(clustered).nodes).toHaveLength(10);
  });
});
