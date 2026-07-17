/**
 * The §6.5 grid's view-model: one row per agent, assembled from the three
 * sources that actually hold the answer.
 *
 * Kept pure and out of the components for the usual reason — this is where the
 * decisions are, and a function is testable where a card is not.
 */

import type { AgentDefinition, AgentOut } from "@titan/api-client";

import {
  activeRun,
  agentState,
  currentTaskId,
  elapsedMs,
  lastFailure,
  latestRun,
  modelInUse,
  rollup,
  runsByAgent,
  throughput,
  type AgentState,
  type ModelInUse,
  type Rollup,
  type Throughput,
} from "./derive";
import {
  definitionFor,
  definitionsById,
  groupByTier,
  tierFor,
  type TierGroup,
} from "./roster";
import type { AgentTier, ProjectRun } from "./types";

export interface AgentView {
  readonly agent: AgentOut;
  /** Null when the agent's role has no roster entry — the card still renders. */
  readonly definition: AgentDefinition | null;
  readonly tier: AgentTier;
  readonly state: AgentState;
  /** From the agent's run. `agents.current_task_id` is never written. */
  readonly currentTaskId: string | null;
  readonly model: ModelInUse;
  /** The last COMPLETED run's average — never a live rate. See `derive`. */
  readonly throughput: Throughput | null;
  readonly elapsedMs: number | null;
  /** The newest failed/timed-out run, for the error card and its link to logs. */
  readonly failure: ProjectRun | null;
  /** The run to open for "show me this agent's logs" (TITAN-FRT-027): whatever
   *  it is doing now, else whatever it did last. */
  readonly logRunId: string | null;
  /**
   * The project the agent's work is IN — which is not always the project the
   * agent is "in". An org-level agent has `project_id` null and can still own
   * runs anywhere, so a task link built from the agent's own column would be
   * missing on exactly the cards that are busiest.
   */
  readonly runProjectId: string | null;
}

export interface AgentFilters {
  readonly role?: string | null;
  readonly state?: AgentState | null;
}

/** Build one agent's row. */
export function agentView(
  agent: AgentOut,
  runs: readonly ProjectRun[],
  byRosterId: ReadonlyMap<string, AgentDefinition>,
  nowMs: number,
): AgentView {
  const state = agentState(agent, runs);
  const failure = lastFailure(runs);
  const active = currentTaskId(runs);
  return {
    agent,
    definition: definitionFor(agent, byRosterId),
    tier: tierFor(agent, byRosterId),
    state,
    currentTaskId: active,
    model: modelInUse(agent, runs),
    throughput: throughput(runs),
    elapsedMs: elapsedMs(runs, nowMs),
    failure,
    // `latestRun` sorts; indexing `runs[0]` would silently trust the caller to
    // have handed them over newest-first.
    logRunId: failure?.id ?? latestRun(runs)?.id ?? null,
    // From the RUN, not the agent: see the field docs.
    runProjectId:
      (activeRun(runs) ?? latestRun(runs))?.project_id ?? agent.project_id,
  };
}

/** Every agent's row, in the API's order. */
export function agentViews(
  agents: readonly AgentOut[],
  definitions: readonly AgentDefinition[],
  runs: readonly ProjectRun[],
  nowMs: number,
): AgentView[] {
  const byRosterId = definitionsById(definitions);
  const byAgent = runsByAgent(runs);
  return agents.map((agent) =>
    agentView(agent, byAgent.get(agent.id) ?? [], byRosterId, nowMs),
  );
}

/** §6.5's "Filter: role ▾" plus a state filter. */
export function filterViews(
  views: readonly AgentView[],
  filters: AgentFilters,
): AgentView[] {
  return views.filter(
    (v) =>
      (filters.role == null || v.agent.role === filters.role) &&
      (filters.state == null || v.state === filters.state),
  );
}

/** The tier sections the grid renders. */
export function groupViews(
  views: readonly AgentView[],
): TierGroup<AgentView>[] {
  return groupByTier(views, (v) => v.tier);
}

/**
 * The header's roll-up.
 *
 * Deliberately computed over the UNFILTERED views: "12 working · 2 idle · 1
 * error" is a statement about the organization, and it would be a lie if
 * choosing "Filter: role = qa" quietly rewrote it to describe only QA. The
 * filter narrows the cards, not the truth above them.
 */
export function rollupOf(views: readonly AgentView[]): Rollup {
  return rollup(views.map((v) => v.state));
}

/** The distinct roles present, for the filter menu. */
export function rolesPresent(views: readonly AgentView[]): string[] {
  return [...new Set(views.map((v) => v.agent.role))].sort();
}

/**
 * Whether the grid must look beyond the agents' own projects for runs.
 *
 * True when any agent is org-level (`project_id` null). Such an agent is NOT a
 * run-less template: `agents.project_id` is nullable, `POST /api/v1/agents`
 * defaults it to None, and nothing ties a run's project to its agent's — a run
 * belongs to a TASK's project, and `agent_in_project` permits an org-level agent
 * to work anywhere. This phase's own backend tests create exactly that shape.
 *
 * So deriving the fetch list from `agents.project_id` alone would render every
 * org-level agent permanently idle while it was demonstrably mid-run. The grid
 * asks the project list instead.
 */
export function needsAllProjects(agents: readonly AgentOut[]): boolean {
  return agents.some((a) => a.project_id == null);
}

/** The projects named by the agents themselves — enough only when every agent
 *  is project-bound (see {@link needsAllProjects}). */
export function projectIdsOf(agents: readonly AgentOut[]): string[] {
  return [
    ...new Set(
      agents.map((a) => a.project_id).filter((p): p is string => p != null),
    ),
  ];
}

/**
 * Every project whose runs the grid needs.
 *
 * The union of the agents' own projects and — when any agent is org-level —
 * every project that exists, since an org-level agent's runs could be in any of
 * them.
 */
export function runProjectIds(
  agents: readonly AgentOut[],
  allProjectIds: readonly string[],
): string[] {
  return needsAllProjects(agents)
    ? [...new Set([...projectIdsOf(agents), ...allProjectIds])]
    : projectIdsOf(agents);
}
