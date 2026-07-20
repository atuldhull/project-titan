/**
 * The canvas's spoken names (Vol 4 §9, TITAN-FRT-043).
 *
 * These strings are the entire accessible presence of a React Flow surface that
 * cannot be rendered in this repo at all, so they are the only part of the
 * canvas that any test can reach. What makes them worth pinning is that the
 * failure mode is not a crash or a blank — React Flow labels nodes and edges by
 * default, with their ids, and every id in both graphs is a UUID. An automated
 * checker sees a name and passes; a human hears thirty-six characters of hex.
 */

import { describe, expect, it } from "vitest";

import {
  degrees,
  edgeAriaLabel,
  labelsById,
  nodeAriaLabel,
} from "@/lib/graph/aria";
import type { GraphEdge, GraphNode } from "@/lib/graph/model";

const EDGES: GraphEdge[] = [
  { from: "a", to: "b" },
  { from: "a", to: "c" },
  { from: "b", to: "c", blocked: true },
];

describe("degrees", () => {
  it("counts edges in both directions", () => {
    const { incoming, outgoing } = degrees(EDGES);
    expect(outgoing.get("a")).toBe(2);
    expect(incoming.get("c")).toBe(2);
    expect(incoming.get("b")).toBe(1);
  });

  it("leaves a node with no edges absent rather than at zero", () => {
    // The caller defaults to 0. This pins that `degrees` does not invent an
    // entry, so a graph of 300 isolated nodes does not allocate 300 zeroes.
    const { incoming, outgoing } = degrees(EDGES);
    expect(incoming.has("a")).toBe(false);
    expect(outgoing.has("c")).toBe(false);
  });

  it("counts nothing from no edges", () => {
    const { incoming, outgoing } = degrees([]);
    expect(incoming.size).toBe(0);
    expect(outgoing.size).toBe(0);
  });
});

describe("nodeAriaLabel", () => {
  it("names the node and states its connections in both directions", () => {
    expect(nodeAriaLabel("Payment API", "task", 2, 1)).toBe(
      "Payment API, task, depends on 2 nodes, required by 1 node.",
    );
  });

  it("says 'nothing' rather than falling silent at zero", () => {
    // Silence is ambiguous between "no dependencies" and "this label was never
    // built" — and the second is the bug this whole module exists to fix, so it
    // must not be indistinguishable from the first.
    expect(nodeAriaLabel("Schema DB", "task", 0, 0)).toBe(
      "Schema DB, task, depends on nothing, required by nothing.",
    );
  });

  it("agrees with itself about singular and plural", () => {
    expect(nodeAriaLabel("X", "component", 1, 2)).toContain(
      "depends on 1 node,",
    );
    expect(nodeAriaLabel("X", "component", 1, 2)).toContain(
      "required by 2 nodes.",
    );
  });

  it("carries the node kind, so a cluster is not read as a task", () => {
    // Past ADR-FRT-04's 500-node guard the canvas renders GROUPS, and a group
    // that announces itself identically to a task sends the user hunting for a
    // task that is not there.
    expect(nodeAriaLabel("auth", "cluster", 0, 3)).toContain("auth, cluster,");
  });
});

describe("edgeAriaLabel", () => {
  it("reads in dependency order, not in arrow order", () => {
    // "Edge from A to B" describes the drawing. "B depends on A" describes the
    // fact the reader came for.
    expect(edgeAriaLabel("Schema DB", "Auth API", false)).toBe(
      "Auth API depends on Schema DB.",
    );
  });

  it("says a blocked edge is blocked — the dashed red line has no other voice", () => {
    // §6.4 renders these dashed and red. TITAN-FRT-044 forbids that being the
    // only signal, and an edge has no text anywhere else on the canvas.
    expect(edgeAriaLabel("Schema DB", "Auth API", true)).toBe(
      "Auth API is blocked by Schema DB.",
    );
  });
});

describe("labelsById", () => {
  const NODES: GraphNode<unknown>[] = [
    {
      id: "0192f4a1-1111-7000-8000-000000000001",
      kind: "task",
      label: "Auth",
      data: {},
    },
  ];

  it("maps ids to labels so an edge can name its ends", () => {
    expect(labelsById(NODES).get(NODES[0].id)).toBe("Auth");
  });

  it("has no entry for an id the model does not contain", () => {
    // `toEdges` falls back to the raw id. That is a UUID read aloud, which is
    // bad — but an edge naming a node that is not in the model is already a
    // broken model, and inventing a friendly name would hide it.
    expect(labelsById(NODES).get("ghost")).toBeUndefined();
  });
});
