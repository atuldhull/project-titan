"use client";

import { useQueries } from "@tanstack/react-query";

import { unwrap, type ProjectOut, type TaskOut } from "@titan/api-client";

import { useAgents } from "@/lib/agents/hooks";
import { useProjects } from "@/lib/projects/hooks";
import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";
import type { PaletteSources } from "./items";

/**
 * The palette's data, fetched only while it is open.
 *
 * Screens and projects are cheap and reused from the same cache entries the
 * Projects and Agents screens fill, so ⌘K on a warm app is instant.
 *
 * TASKS ARE THE HARD PART, and the honest shape of the problem is:
 * there is NO global task list and no cross-entity search endpoint. `GET
 * /projects/{id}/tasks` is the only list route (`orchestrator/router.py`), and
 * it has no text filter. So "search any task" (TITAN-FRT-007) can only be a
 * client-side fan-out over projects, exactly as the org Deployments screen and
 * the agent grid already fan out for the same reason.
 *
 * That fan-out is bounded on purpose:
 *  - it does not start until the query is specific enough to be worth N requests;
 *  - it covers the most recent {@link TASK_FANOUT_PROJECTS} projects, not all of
 *    them, because each project costs up to 50 paginated requests;
 *  - and the palette SAYS SO when the bound is in effect, rather than quietly
 *    presenting a partial index as if it were complete.
 */

/** Fan-out width. Projects arrive newest-first, so this is "recent work". */
export const TASK_FANOUT_PROJECTS = 8;

/** Below this, a task search is too broad to be worth the requests. */
const MIN_TASK_QUERY = 2;

export interface PaletteCandidates extends PaletteSources {
  /** True when tasks were searched across fewer projects than exist. */
  readonly tasksTruncated: boolean;
  readonly tasksLoading: boolean;
  /** True when every task request failed — NOT the same as "no matches". */
  readonly tasksFailed: boolean;
}

export function usePaletteCandidates(
  open: boolean,
  query: string,
  /** ⌘P searches projects only; fanning out for tasks there is pure waste. */
  wantsTasks = true,
): PaletteCandidates {
  const projects = useProjects({ enabled: open });
  const agents = useAgents({ enabled: open });

  const projectList = projects.data?.items ?? [];
  const wantTasks = wantsTasks && open && query.trim().length >= MIN_TASK_QUERY;
  const searched = wantTasks ? projectList.slice(0, TASK_FANOUT_PROJECTS) : [];

  const taskQueries = useQueries({
    queries: searched.map((project) => ({
      /*
       * A key of the palette's OWN, deliberately not `queryKeys.projectTasksAll`.
       *
       * Sharing that key looked like a free cache hit and was actually cache
       * poisoning: `lib/graph/hooks.ts` fills it by DRAINING every page (up to
       * 50 requests) because the task graph must be complete, while this fetch
       * takes a single page of 200 and discards `next_cursor`. Whichever ran
       * first won, so opening ⌘K and then visiting the graph within the stale
       * time showed a silently truncated graph — missing nodes and missing
       * edges, with no error anywhere.
       */
      queryKey: [...queryKeys.projectTasksAll(project.id), "palette-page"],
      queryFn: async (): Promise<{ items: TaskOut[] }> => {
        const result = unwrap(
          await browserApi().GET("/api/v1/projects/{project_id}/tasks", {
            params: { path: { project_id: project.id }, query: { limit: 200 } },
          }),
        );
        return { items: result.items };
      },
    })),
  });

  // Rebuilt each render rather than memoised. The palette re-renders per
  // keystroke, so this and the `buildItems`/`rank` pass downstream all run per
  // character — measured at well under a millisecond for a few hundred rows,
  // against a 100 ms budget. Memoising it needs a stable key across a
  // variable-length `useQueries` result, which is more machinery than the cost
  // justifies; revisit if the fan-out width ever grows.
  const tasks: { task: TaskOut; project: ProjectOut }[] = [];
  taskQueries.forEach((result, index) => {
    const project = searched[index];
    if (!project) {
      return;
    }
    for (const task of result.data?.items ?? []) {
      tasks.push({ task, project });
    }
  });

  return {
    projects: projectList,
    agents: agents.data?.items ?? [],
    tasks,
    tasksTruncated: wantTasks && projectList.length > TASK_FANOUT_PROJECTS,
    tasksLoading: taskQueries.some((result) => result.isLoading),
    // A failed fan-out is not an empty one. Reporting "No matches" when every
    // request 500'd tells the operator their task does not exist — the third
    // time this project has shipped that exact confusion (Phase 63's $0.00,
    // Phase 64's "No plugins installed" on a 403).
    tasksFailed:
      wantTasks &&
      taskQueries.length > 0 &&
      taskQueries.every((result) => result.isError),
  };
}
