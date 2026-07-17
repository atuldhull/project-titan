/**
 * The shared graph model (ADR-FRT-04).
 *
 * ONE engine serves two very different domains — the task DAG (§6.4) and the
 * architecture (§6.8) — so the engine must not know about either. This is the
 * narrow shape both reduce to: nodes with a `kind` (which selects a renderer)
 * and directed edges. Everything domain-specific rides in `data`, which the
 * engine only hands back to the node component that asked for it.
 *
 * Pure: no React, no React Flow, no fetch. The layout, the clustering guard, the
 * critical path and the diff are all functions over these types, which is what
 * makes them testable without a browser — and a browser is the one thing this
 * repo has no way to run.
 */

/** A directed edge: `from` must come before `to`. */
export interface GraphEdge {
  readonly from: string;
  readonly to: string;
  /** Renders dashed-red: this edge is why `to` cannot start (§6.4). */
  readonly blocked?: boolean;
}

export interface GraphNode<Data = unknown> {
  readonly id: string;
  /** Selects the renderer from the node-type registry. */
  readonly kind: string;
  readonly label: string;
  /** Domain payload — opaque to the engine. */
  readonly data: Data;
}

export interface GraphModel<Data = unknown> {
  readonly nodes: readonly GraphNode<Data>[];
  readonly edges: readonly GraphEdge[];
}

/** A node's position after layout. */
export interface Positioned<Data = unknown> extends GraphNode<Data> {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

export interface LaidOut<Data = unknown> {
  readonly nodes: readonly Positioned<Data>[];
  readonly edges: readonly GraphEdge[];
}

/** Adjacency in both directions — the basis of every traversal below. */
export interface Adjacency {
  /** node -> nodes it points AT (its successors). */
  readonly out: ReadonlyMap<string, readonly string[]>;
  /** node -> nodes pointing at IT (its predecessors). */
  readonly in: ReadonlyMap<string, readonly string[]>;
}

export function adjacency(model: GraphModel): Adjacency {
  const out = new Map<string, string[]>();
  const incoming = new Map<string, string[]>();
  for (const node of model.nodes) {
    out.set(node.id, []);
    incoming.set(node.id, []);
  }
  for (const edge of model.edges) {
    // An edge to a node outside the model is dropped rather than materialising
    // a phantom: the layout would place a node nothing can select or inspect.
    if (!out.has(edge.from) || !out.has(edge.to)) {
      continue;
    }
    out.get(edge.from)?.push(edge.to);
    incoming.get(edge.to)?.push(edge.from);
  }
  return { out, in: incoming };
}

/**
 * Every node reachable from `start`, following `direction`. Excludes `start`.
 *
 * Iterative and visited-guarded: a task DAG is acyclic by construction (the
 * server rejects a dependency that closes a cycle), but the architecture graph
 * is `depends_on` strings an agent wrote, with nothing checking for cycles — so
 * a recursive walk here would blow the stack on live data.
 */
export function reachable(
  adj: Adjacency,
  start: string,
  direction: "downstream" | "upstream",
): ReadonlySet<string> {
  const edges = direction === "downstream" ? adj.out : adj.in;
  const seen = new Set<string>();
  const stack = [...(edges.get(start) ?? [])];
  while (stack.length > 0) {
    const id = stack.pop();
    if (id === undefined || seen.has(id)) {
      continue;
    }
    seen.add(id);
    stack.push(...(edges.get(id) ?? []));
  }
  seen.delete(start); // a cycle can walk back to where it started
  return seen;
}

/** Both chains through a node — what §6.4 highlights on hover. */
export function connectedChains(
  adj: Adjacency,
  nodeId: string,
): ReadonlySet<string> {
  const chains = new Set<string>([nodeId]);
  for (const id of reachable(adj, nodeId, "downstream")) {
    chains.add(id);
  }
  for (const id of reachable(adj, nodeId, "upstream")) {
    chains.add(id);
  }
  return chains;
}
