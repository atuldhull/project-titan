"use client";

/**
 * Data for the two graph screens.
 *
 * Fetching is React Query throughout (TITAN-FRT-014). Note how much of §6.4's
 * node has to be assembled here: the graph endpoint returns `{id, title, status}`
 * per node and nothing else, so the assignee avatar and the milestone grouping
 * both come from joining the task list client-side. That is not a shortcut —
 * there is no endpoint that returns a task graph with either.
 */

import { useQuery } from "@tanstack/react-query";

import { unwrap } from "@titan/api-client";
import type { components } from "@titan/schemas";

import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";

export type ArtifactVersions = components["schemas"]["ArtifactVersions"];
export type HandoffArtifactOut = components["schemas"]["HandoffArtifactOut"];
export type DecisionOut = components["schemas"]["DecisionOut"];

/** The stage whose artifact IS the architecture (Vol 6 §2.3 H5). */
export const ARCHITECTURE_STAGE = "architect_design";

/**
 * Tasks, for the two fields the graph endpoint omits: assignee and milestone.
 *
 * **This drains every page.** 200 is the API's maximum `limit`, and `/graph` is
 * unpaginated — so a single page silently mismatches the two sources on exactly
 * the projects that need help most. Past 200 tasks, an unfetched task has no
 * entry in the assignee map and the inspector calls it "Unassigned" (a claim,
 * not an absence), and it has no milestone either, so it collapses into one
 * bogus "No milestone" cluster — precisely at the >500-node sizes where the
 * clustering guard is supposed to be doing the work. Clustering is not a
 * mitigation for the cap; it is a victim of it. Hence the drain.
 */
export function useProjectTasks(pid: string) {
  return useQuery({
    queryKey: queryKeys.projectTasksAll(pid),
    queryFn: async () => {
      const client = browserApi();
      const items: components["schemas"]["TaskOut"][] = [];
      let cursor: string | undefined;
      // Bounded: /graph itself is unbounded, so the honest ceiling is "as many
      // pages as there are", with a guard so a broken cursor cannot spin.
      for (let page = 0; page < 50; page += 1) {
        const result = unwrap(
          await client.GET("/api/v1/projects/{project_id}/tasks", {
            params: {
              path: { project_id: pid },
              query: cursor ? { limit: 200, cursor } : { limit: 200 },
            },
          }),
        );
        items.push(...result.items);
        cursor = result.next_cursor ?? undefined;
        if (!cursor) {
          break;
        }
      }
      return { items };
    },
  });
}

/**
 * The architecture's revision history — every version the Architect committed.
 *
 * Reads the seam this phase added: the rows always existed (a resubmission is a
 * new version, never an overwrite) but only the newest was fetchable.
 */
export function useArtifactVersions(pid: string, stage: string) {
  return useQuery<ArtifactVersions>({
    queryKey: queryKeys.artifactVersions(pid, stage),
    queryFn: async () =>
      unwrap(
        await browserApi().GET(
          "/api/v1/projects/{project_id}/handoffs/{stage}/artifact-versions",
          { params: { path: { project_id: pid, stage } } },
        ),
      ),
  });
}

/** One revision, in full. Two of these are what a diff is a function of. */
export function useArtifactVersion(
  pid: string,
  stage: string,
  version: number | null,
) {
  return useQuery<HandoffArtifactOut>({
    enabled: version !== null,
    // A version is immutable — it is never overwritten — so it never goes stale.
    staleTime: Infinity,
    gcTime: Infinity,
    queryKey: queryKeys.artifactVersion(pid, stage, version ?? 0),
    queryFn: async () =>
      unwrap(
        await browserApi().GET(
          "/api/v1/projects/{project_id}/handoffs/{stage}/artifact-versions/{version}",
          {
            params: { path: { project_id: pid, stage, version: version ?? 0 } },
          },
        ),
      ),
  });
}

/**
 * The ADR panel's records (§6.8: "Context/Decision/Alternatives/Trade-offs
 * rendered from backend records").
 *
 * The `decisions` table is the right source and carries all four — "Trade-offs"
 * is stored as `consequences`, a rename not a gap. Note this is a DIFFERENT,
 * richer record from `ArchitecturePackage.adrs`, which is only
 * `{id, title, decision, rationale}`; the panel prefers this one and says so.
 */
export function useDecisions(pid: string) {
  return useQuery({
    queryKey: queryKeys.projectDecisions(pid),
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/projects/{project_id}/decisions", {
          params: { path: { project_id: pid }, query: { limit: 100 } },
        }),
      ),
  });
}
