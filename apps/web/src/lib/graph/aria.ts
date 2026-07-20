/**
 * The spoken names of the canvas's nodes and edges (Vol 4 §9, TITAN-FRT-043).
 *
 * Pure, and in `lib/graph` rather than beside the canvas for the reason
 * `graph-canvas.tsx` states about itself: a React Flow surface cannot be
 * rendered in this repo at all (jsdom has no ResizeObserver and no layout), so
 * anything left inside that component is untestable by construction. An
 * accessible name is exactly the kind of thing that is silently wrong for a year
 * — nobody sees it — so it is a string builder with a test.
 *
 * Why they need to exist at all: React Flow does label nodes and edges by
 * default, as `"<id>"` and `"Edge from <id> to <id>"`. Every id in both graphs is
 * a UUID. So the default is not a missing label, it is a label that reads as
 * thirty-six characters of hexadecimal, twice — which is worse than silence,
 * because a checker sees a name and passes it.
 */

import type { GraphEdge, GraphNode } from "@/lib/graph/model";

/** How many edges arrive at / leave each node. */
export function degrees(edges: readonly GraphEdge[]): {
  incoming: ReadonlyMap<string, number>;
  outgoing: ReadonlyMap<string, number>;
} {
  const incoming = new Map<string, number>();
  const outgoing = new Map<string, number>();
  for (const edge of edges) {
    outgoing.set(edge.from, (outgoing.get(edge.from) ?? 0) + 1);
    incoming.set(edge.to, (incoming.get(edge.to) ?? 0) + 1);
  }
  return { incoming, outgoing };
}

/**
 * A node's accessible name.
 *
 * The degree counts are part of it deliberately. On the canvas an edge is SVG
 * geometry: it has a name of its own but no reading order that ties it to either
 * end, so a node that does not state its own connections is announced as an
 * isolated box — and "what blocks this task" is the single question §6.4 exists
 * to answer. Zero is stated as "nothing" rather than omitted, because a silent
 * node is ambiguous between "no dependencies" and "this label was not built".
 * The List view remains the full alternative (TITAN-FRT-047); this is what makes
 * the canvas itself not actively misleading.
 */
export function nodeAriaLabel(
  label: string,
  kind: string,
  incoming: number,
  outgoing: number,
): string {
  const dependsOn =
    incoming === 0
      ? "depends on nothing"
      : `depends on ${incoming} ${incoming === 1 ? "node" : "nodes"}`;
  const requiredBy =
    outgoing === 0
      ? "required by nothing"
      : `required by ${outgoing} ${outgoing === 1 ? "node" : "nodes"}`;
  return `${label}, ${kind}, ${dependsOn}, ${requiredBy}.`;
}

/**
 * An edge's accessible name, read in the direction the reader cares about.
 *
 * "B depends on A", not "edge from A to B": the arrow's direction is a rendering
 * detail, the dependency is the fact. A blocked edge says so in words — §6.4
 * renders it dashed and red, which is a colour-and-texture signal that
 * TITAN-FRT-044 requires be paired with text somewhere, and this is the only
 * place an edge has any.
 */
export function edgeAriaLabel(
  fromLabel: string,
  toLabel: string,
  blocked: boolean,
): string {
  return blocked
    ? `${toLabel} is blocked by ${fromLabel}.`
    : `${toLabel} depends on ${fromLabel}.`;
}

/** Node id → label, for naming an edge by its ends rather than their UUIDs. */
export function labelsById(
  nodes: readonly GraphNode<unknown>[],
): ReadonlyMap<string, string> {
  return new Map(nodes.map((node) => [node.id, node.label]));
}
