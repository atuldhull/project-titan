/**
 * Task dependencies, derived from the project graph.
 *
 * There is no per-task dependency endpoint — `/tasks/{id}/dependencies` is
 * POST-only — so the one readable source is `GET /projects/{pid}/graph`, which
 * returns every node and edge in the project. The Sheet and the blocked badge
 * both derive from it client-side.
 *
 * The satisfied rule mirrors the server's `dependencies_all_done` EXACTLY:
 *
 *     SELECT NOT EXISTS (... WHERE td.task_id = :tid AND d.status <> 'done')
 *
 * Note what that means and what it does not: `done` alone satisfies a dependency.
 * A `cancelled` blocker does NOT — it blocks forever until the edge is removed.
 * That is deliberate on the server's side, so it is deliberate here: this is the
 * predicate that decides whether a `ready` drag will 409, and a client that
 * counted `cancelled` as satisfied would promise a move the server refuses.
 *
 * Pure: no React, no fetch.
 */

import type { components } from "@titan/schemas";

export type Graph = components["schemas"]["Graph"];
export type GraphNode = components["schemas"]["GraphNode"];

/** The one status that satisfies a dependency, per the server's own SQL. */
const SATISFIED = "done";

export interface Dependency {
  readonly node: GraphNode;
  readonly satisfied: boolean;
}

export interface TaskDependencies {
  /** Tasks this one depends on — what must finish first. */
  readonly blockedBy: readonly Dependency[];
  /** Tasks that depend on this one — what it is holding up. */
  readonly blocking: readonly GraphNode[];
}

/** Empty, for a graph that has not loaded (or a task with no edges). */
export const NO_DEPENDENCIES: TaskDependencies = {
  blockedBy: [],
  blocking: [],
};

export function taskDependencies(
  graph: Graph | undefined,
  taskId: string,
): TaskDependencies {
  if (!graph) {
    return NO_DEPENDENCIES;
  }
  const nodes = new Map(graph.nodes.map((n) => [n.id, n]));
  const blockedBy: Dependency[] = [];
  const blocking: GraphNode[] = [];

  for (const edge of graph.edges) {
    if (edge.task_id === taskId) {
      const node = nodes.get(edge.depends_on_id);
      if (node) {
        blockedBy.push({ node, satisfied: node.status === SATISFIED });
      }
    } else if (edge.depends_on_id === taskId) {
      const node = nodes.get(edge.task_id);
      if (node) {
        blocking.push(node);
      }
    }
  }
  return { blockedBy, blocking };
}

/**
 * Task id -> how many of its dependencies are not yet done.
 *
 * This is the card's "blocked" badge, and it is a real signal rather than a
 * cosmetic one: a task with any unmet dependency is exactly a task the server
 * will refuse to mark `ready`. It is a different thing from the `blocked` STATUS
 * (a column of its own, entered deliberately via the `block` action) — a task can
 * be either, both, or neither.
 */
export function unmetDependencyCounts(
  graph: Graph | undefined,
): ReadonlyMap<string, number> {
  const counts = new Map<string, number>();
  if (!graph) {
    return counts;
  }
  const status = new Map(graph.nodes.map((n) => [n.id, n.status]));
  for (const edge of graph.edges) {
    const blockerStatus = status.get(edge.depends_on_id);
    // An edge to a task outside the graph cannot be evaluated. The server's
    // query joins, so a missing row cannot satisfy it either — count it unmet
    // rather than quietly treating "unknown" as "done".
    if (blockerStatus !== SATISFIED) {
      counts.set(edge.task_id, (counts.get(edge.task_id) ?? 0) + 1);
    }
  }
  return counts;
}
