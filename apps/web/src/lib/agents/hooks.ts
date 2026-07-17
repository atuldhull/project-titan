"use client";

/**
 * Data for the §6.5 agent grid and its detail drawer.
 *
 * The shape of this file is dictated by one fact: an agent's live work lives in
 * `runs`, not `agents` (see `./derive`). Runs are project-scoped — a run
 * authorizes through its project (TITAN-SEC-032) — while agents are org-level,
 * so the org-wide grid fans out per project. That is the same pattern the
 * Overview already uses for boards and approvals.
 */

import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { unwrap, type AgentOut } from "@titan/api-client";

import type { ProjectRun } from "./types";

import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";

/** The API's own ceiling (`MAX_LIMIT`), not a number we chose. */
const MAX_PAGE = 200;

/**
 * How often the grid re-reads runs while it is open.
 *
 * Two things make this necessary rather than belt-and-braces, and both are
 * absences in the backend rather than preferences:
 *
 * 1. **A timed-out run emits no frame.** `_emit_run_event` has exactly two call
 *    sites — `run.started` and `run.finished`. The soft-timeout path marks the
 *    run `timed_out` and nacks WITHOUT publishing, so a socket-only grid keeps
 *    the stale `running` row forever and the agent reads "working" for good.
 * 2. **Elapsed needs a clock.** Nothing else re-renders a card between
 *    `run.started` and `run.finished`, so a once-sampled `Date.now()` freezes at
 *    ~0s for the whole run.
 *
 * 10s is coarse on purpose: the readout is only meaningful to the nearest few
 * seconds, and this is a fallback for the frames that never come — not the
 * primary path, which is still push.
 */
const RUNS_REFRESH_MS = 10_000;

/** The RUNTIME registry: id, role, name, status, model_binding, roster_id. */
export function useAgents() {
  return useQuery({
    queryKey: queryKeys.agents(),
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/agents", {
          params: { query: { limit: MAX_PAGE } },
        }),
      ),
  });
}

/**
 * The DECLARATIVE roster: tier, supervisor, KPIs, model policy.
 *
 * A different endpoint, shape and lifetime from `useAgents` — and, until Phase
 * 61, the same cache key, which is why the roster intermittently received a
 * `Page` and threw on `.filter`. These are static YAML contracts: they never
 * change at runtime, so nothing invalidates this key.
 */
export function useAgentDefinitions() {
  return useQuery({
    queryKey: queryKeys.agentDefinitions(),
    queryFn: async () =>
      unwrap(await browserApi().GET("/api/v1/agent-definitions")),
    staleTime: Infinity,
  });
}

export interface AcrossProjects {
  runs: ProjectRun[];
  isLoading: boolean;
  isError: boolean;
}

/**
 * Hoisted to module scope, not inlined into the hook.
 *
 * React Query only memoizes a `combine` result while the function itself is
 * referentially stable. An inline arrow is a new function every render, so the
 * result would be a new object every render too — and since the grid's
 * `agentViews` memo keys on `runs`, every `useMemo` downstream of it would
 * recompute on every render, quietly defeating all of them. This closes over
 * nothing, so module scope costs nothing.
 *
 * A project whose runs fail to load contributes nothing rather than taking the
 * whole grid down: §6.5 says an unreachable corner degrades its own cards and
 * the grid never blanks.
 */
function combineRuns(
  queries: readonly {
    data?: { items: ProjectRun[] };
    isLoading: boolean;
    isError: boolean;
  }[],
): AcrossProjects {
  return {
    runs: queries.flatMap((q) => q.data?.items ?? []),
    // `some` on an empty array is false, so a caller with no projects is
    // settled-and-empty, never a permanent skeleton.
    isLoading: queries.some((q) => q.isLoading),
    isError: queries.some((q) => q.isError),
  };
}

/** Recent runs across several projects at once, flattened. */
export function useRunsAcrossProjects(
  projectIds: readonly string[],
): AcrossProjects {
  return useQueries({
    queries: projectIds.map((pid) => ({
      queryKey: queryKeys.projectRuns(pid),
      queryFn: async (): Promise<{ items: ProjectRun[] }> => {
        const page = unwrap(
          await browserApi().GET("/api/v1/projects/{project_id}/runs", {
            params: { path: { project_id: pid }, query: { limit: MAX_PAGE } },
          }),
        );
        // Stamp the project the fetch already knows: RunOut does not carry one.
        return {
          items: page.items.map((run) => ({ ...run, project_id: pid })),
        };
      },
      refetchInterval: RUNS_REFRESH_MS,
    })),
    combine: combineRuns,
  });
}

/**
 * One agent's own messages — by ROLE, which is how the system attributes them.
 *
 * Filtering on `sender_agent_id` looks obviously right and is nearly useless:
 * `bus.send` writes `sender_agent_id=None` explicitly ("bus messages identify
 * their sender by role"), so EVERY message an agent emits during a turn is
 * invisible to it. The id column is populated only by actor-driven transitions.
 *
 * `from_agent` holds the roster id (`turn.py` passes the agent's roster id
 * straight through), which is what `AgentOut.roster_id` resolves to — so this
 * asks by role and gets the agent's real output.
 *
 * The honest cost: two agents sharing a role are indistinguishable here. That is
 * the system's own attribution model, not something this query can fix, and the
 * drawer says so rather than implying the list is one agent's alone.
 */
export function useAgentMessages(
  projectId: string | null,
  rosterId: string | null,
) {
  const enabled = projectId !== null && rosterId !== null;
  return useQuery({
    queryKey:
      projectId && rosterId
        ? queryKeys.agentMessages(projectId, rosterId)
        : ["project", "none", "messages", "agent", rosterId ?? "none"],
    enabled,
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/projects/{project_id}/messages", {
          params: {
            path: { project_id: projectId as string },
            query: { from_agent: rosterId as string, limit: 20 },
          },
        }),
      ),
  });
}

/**
 * Pause / resume.
 *
 * §6.5 asks for Pause, Restart and Reassign. Only pause/resume exist: there is
 * no restart endpoint and no reassign action (assignment is the orchestrator's
 * job — `AgentPatch` forbids `current_task_id` outright). Buttons for the other
 * two would be dead controls, so they are not rendered.
 *
 * Both are `agent:configure`, which only owner and admin hold — matching §6.5's
 * "RBAC admin+".
 */
export function useAgentLifecycle() {
  const client = useQueryClient();
  const invalidate = (agent: AgentOut) => {
    void client.invalidateQueries({ queryKey: queryKeys.agents() });
    void client.invalidateQueries({ queryKey: queryKeys.agent(agent.id) });
  };

  const pause = useMutation({
    mutationFn: async (agentId: string) =>
      unwrap(
        await browserApi().POST("/api/v1/agents/{agent_id}:pause", {
          params: { path: { agent_id: agentId } },
        }),
      ),
    onSuccess: invalidate,
  });

  const resume = useMutation({
    mutationFn: async (agentId: string) =>
      unwrap(
        await browserApi().POST("/api/v1/agents/{agent_id}:resume", {
          params: { path: { agent_id: agentId } },
        }),
      ),
    onSuccess: invalidate,
  });

  return { pause, resume };
}
