/**
 * The task DAG as the shared engine sees it (Vol 4 §6.4).
 *
 * Source: `GET /projects/{pid}/graph` → `{nodes: [{id, title, status}], edges:
 * [{task_id, depends_on_id}]}`. That is the whole of it — the node carries THREE
 * fields. §6.4's card wants a status icon, an ID, a title and an agent avatar;
 * the first three are here and the assignee is not, because the graph SELECT
 * never projects it (the column exists on `tasks`). The screen joins the task
 * list client-side for avatars rather than this module inventing one.
 *
 * Pure: no React, no fetch.
 */

import type { components } from "@titan/schemas";

import type { GraphModel, GraphNode } from "@/lib/graph/model";
import { adjacency } from "@/lib/graph/model";

export type ApiGraph = components["schemas"]["Graph"];

/** What a task node renders from. */
export interface TaskNodeData {
  readonly status: string;
  /** Unmet dependencies — the same predicate the server gates `ready` on. */
  readonly blockedBy: number;
  /** Resolved client-side from the task list; absent when unknown. */
  readonly assigneeAgentId?: string | null;
  readonly milestoneId?: string | null;
}

/**
 * The one status that satisfies a dependency — the server's own rule
 * (`dependencies_all_done`: `WHERE d.status <> 'done'`). `cancelled` does NOT
 * satisfy: it blocks forever. Kept identical to `lib/kanban/deps.ts` on purpose;
 * if these two ever disagree the board and the graph will claim different things
 * about the same task.
 */
const SATISFIED = "done";

/** States that are finished work, for the critical path's "incomplete" test. */
const COMPLETE = new Set(["done", "merged"]);
/** Deliberately excluded from the critical path: it will never be worked. */
const ABANDONED = new Set(["cancelled"]);

/**
 * Build the engine's model from the API's graph.
 *
 * Edge direction: the API says `task_id depends_on depends_on_id`, so the work
 * flows `depends_on_id -> task_id`. That inversion is deliberate and matches the
 * backend's own DOT renderer; get it backwards and every graph draws mirrored.
 *
 * An edge is `blocked` when its source is not `done` — i.e. it is currently the
 * reason its target cannot proceed. That is what §6.4 renders dashed-red.
 */
export function taskGraphModel(
  graph: ApiGraph | undefined,
  extra: {
    readonly assignees?: ReadonlyMap<string, string | null>;
    readonly milestones?: ReadonlyMap<string, string | null>;
  } = {},
): GraphModel<TaskNodeData> {
  if (!graph) {
    return { nodes: [], edges: [] };
  }
  const status = new Map(graph.nodes.map((n) => [n.id, n.status]));
  const blockedBy = new Map<string, number>();
  for (const edge of graph.edges) {
    if (status.get(edge.depends_on_id) !== SATISFIED) {
      blockedBy.set(edge.task_id, (blockedBy.get(edge.task_id) ?? 0) + 1);
    }
  }

  return {
    nodes: graph.nodes.map((node) => ({
      id: node.id,
      kind: "task",
      label: node.title,
      data: {
        status: node.status,
        blockedBy: blockedBy.get(node.id) ?? 0,
        assigneeAgentId: extra.assignees?.get(node.id) ?? null,
        milestoneId: extra.milestones?.get(node.id) ?? null,
      },
    })),
    edges: graph.edges.map((edge) => ({
      from: edge.depends_on_id,
      to: edge.task_id,
      blocked: status.get(edge.depends_on_id) !== SATISFIED,
    })),
  };
}

/**
 * The critical path: the longest chain of INCOMPLETE work (§6.4).
 *
 * Longest by node count, not duration — the graph has no durations to add up
 * (`estimate_points` is not on a graph node, and points are not time anyway), so
 * a weighted path would be an invention. This is the longest remaining chain of
 * work, which is what the toggle claims and no more.
 *
 * Completed nodes are excluded rather than counted-as-zero: a finished task
 * cannot be on the path of what is still to do. `cancelled` is excluded too — it
 * will never be worked, so a chain through it is not a chain anyone walks.
 *
 * Ties are broken deterministically (first by input order), so the highlight
 * does not flicker between equal paths on re-render.
 */
export function criticalPath(
  model: GraphModel<TaskNodeData>,
): readonly string[] {
  const live = model.nodes.filter(
    (n) => !COMPLETE.has(n.data.status) && !ABANDONED.has(n.data.status),
  );
  const liveIds = new Set(live.map((n) => n.id));
  const adj = adjacency({
    nodes: live,
    edges: model.edges.filter((e) => liveIds.has(e.from) && liveIds.has(e.to)),
  });

  // Longest path from each node, memoised. The task DAG is acyclic (the server
  // rejects a cycle-closing dependency), but `visiting` guards anyway: a cycle
  // would otherwise recurse forever, and this must not be the thing that hangs
  // the tab.
  const best = new Map<string, readonly string[]>();
  const visiting = new Set<string>();

  const longestFrom = (id: string): readonly string[] => {
    const memo = best.get(id);
    if (memo) {
      return memo;
    }
    if (visiting.has(id)) {
      return [id]; // cycle — stop, do not pretend to know the path
    }
    visiting.add(id);
    let longest: readonly string[] = [];
    for (const next of adj.out.get(id) ?? []) {
      const candidate = longestFrom(next);
      if (candidate.length > longest.length) {
        longest = candidate;
      }
    }
    visiting.delete(id);
    const path = [id, ...longest];
    best.set(id, path);
    return path;
  };

  let winner: readonly string[] = [];
  for (const node of live) {
    const path = longestFrom(node.id);
    if (path.length > winner.length) {
      winner = path;
    }
  }
  return winner;
}

/** Group key for the >500-node clustering guard: a task's milestone (§6.4). */
export function milestoneGroup(node: GraphNode<TaskNodeData>): string {
  return node.data.milestoneId ?? "No milestone";
}
