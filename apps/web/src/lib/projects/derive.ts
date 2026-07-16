import type { components } from "@titan/schemas";

/**
 * Derivations for the Overview/Projects screens (Vol 4 §6.1, §6.2).
 *
 * WHY THIS FILE EXISTS: the spec's project card wants progress %, agent count
 * and CI status, and the KPI cards want blocked counts — none of which the
 * backend exposes as a field. `ProjectOut` carries no progress, no updated_at
 * and no CI; `/agents` has no project filter. What DOES exist is the Phase-41
 * board (columns of tasks per workflow state) and `AgentOut.project_id/status`.
 * So progress and blocked counts are derived from the board, and agent counts by
 * grouping the agent list. CI has no source at all and is reported as unknown
 * rather than invented — §6.2's own glyph vocabulary has `CI –` for exactly that.
 *
 * Pure functions, no I/O — unit-tested.
 */

type Board = components["schemas"]["Board"];
type AgentOut = components["schemas"]["AgentOut"];

/** Task workflow states (apps/api/src/titan/orchestrator/transitions.py STATES). */
export const TASK_STATES = [
  "backlog",
  "ready",
  "assigned",
  "in_progress",
  "in_review",
  "changes_requested",
  "done",
  "reopened",
  "cancelled",
  "blocked",
  "in_loop",
  "merged",
  "failed",
  "escalated",
] as const;

/** States that count as delivered work for a progress ratio. */
const COMPLETED_STATES: ReadonlySet<string> = new Set(["done", "merged"]);
/** Excluded from the denominator — cancelled work isn't outstanding work. */
const EXCLUDED_STATES: ReadonlySet<string> = new Set(["cancelled"]);
/** Work that cannot proceed without a human or an unblock. */
const BLOCKED_STATES: ReadonlySet<string> = new Set([
  "blocked",
  "failed",
  "escalated",
]);

export interface BoardStats {
  readonly total: number;
  readonly completed: number;
  readonly blocked: number;
  /** 0-100, or null when there is no work to measure (avoids a fake 0%). */
  readonly progressPct: number | null;
  readonly byState: Readonly<Record<string, number>>;
}

export function boardStats(board: Board | undefined): BoardStats {
  const byState: Record<string, number> = {};
  let total = 0;
  let completed = 0;
  let blocked = 0;

  for (const column of board?.columns ?? []) {
    const count = column.tasks.length;
    byState[column.status] = (byState[column.status] ?? 0) + count;
    if (EXCLUDED_STATES.has(column.status)) {
      continue;
    }
    total += count;
    if (COMPLETED_STATES.has(column.status)) {
      completed += count;
    }
    if (BLOCKED_STATES.has(column.status)) {
      blocked += count;
    }
  }

  return {
    total,
    completed,
    blocked,
    progressPct: total === 0 ? null : Math.round((completed / total) * 100),
    byState,
  };
}

export interface ProjectAgentCounts {
  readonly total: number;
  /** `busy` agents — the spec's "working" (AgentStatus: idle|busy|paused|error). */
  readonly working: number;
}

/**
 * Group agents by project. `/api/v1/agents` has no `project_id` query param, but
 * `AgentOut` carries `project_id`, so one bounded list call is grouped here
 * rather than fanned out per project.
 */
export function agentsByProject(
  agents: readonly AgentOut[],
): Map<string, ProjectAgentCounts> {
  const out = new Map<string, ProjectAgentCounts>();
  for (const agent of agents) {
    if (!agent.project_id) {
      continue; // org-level agent, not attributable to a project card
    }
    const prev = out.get(agent.project_id) ?? { total: 0, working: 0 };
    out.set(agent.project_id, {
      total: prev.total + 1,
      working: prev.working + (agent.status === "busy" ? 1 : 0),
    });
  }
  return out;
}

export function countWorking(agents: readonly AgentOut[]): number {
  return agents.filter((a) => a.status === "busy").length;
}

/**
 * CI status has NO backend source today (no field, no endpoint). §6.2's glyph
 * vocabulary uses `CI –` for none/pending, which is exactly the honest render —
 * we never fabricate a green check.
 */
export type CiStatus = "unknown";

export function ciStatus(): CiStatus {
  return "unknown";
}
