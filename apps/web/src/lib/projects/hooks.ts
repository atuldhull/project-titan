"use client";

import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { unwrap } from "@titan/api-client";

import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";
import { agentsByProject, type ProjectAgentCounts } from "./derive";

/** Server-side sort is limited to `^-?created_at$` — anything else is client-side. */
export type ProjectSort = "-created_at" | "created_at";

export function useProjects(
  options: {
    status?: string;
    sort?: ProjectSort;
    /** For the shell-mounted command palette: fetch only while it is open. */
    enabled?: boolean;
  } = {},
) {
  const { status, sort = "-created_at", enabled = true } = options;
  return useQuery({
    enabled,
    queryKey: [...queryKeys.projects(), { status: status ?? null, sort }],
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/projects", {
          params: { query: { status, sort, limit: 200 } },
        }),
      ),
  });
}

export function useProject(pid: string) {
  return useQuery({
    // The shell's project switcher renders on every screen and passes "" when
    // no project is in the URL; without this it would fire `GET /projects/`.
    enabled: pid !== "",
    queryKey: queryKeys.project(pid),
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/projects/{project_id}", {
          params: { path: { project_id: pid } },
        }),
      ),
  });
}

/** The board is the only real progress/blocked signal (see derive.ts). */
export function useProjectBoard(pid: string, enabled = true) {
  return useQuery({
    enabled,
    queryKey: queryKeys.projectBoard(pid),
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/projects/{project_id}/board", {
          params: { path: { project_id: pid } },
        }),
      ),
  });
}

/** Boards for several projects at once — the Overview needs one per card. */
export function useProjectBoards(pids: readonly string[]) {
  return useQueries({
    queries: pids.map((pid) => ({
      queryKey: queryKeys.projectBoard(pid),
      queryFn: async () =>
        unwrap(
          await browserApi().GET("/api/v1/projects/{project_id}/board", {
            params: { path: { project_id: pid } },
          }),
        ),
    })),
  });
}

/** One bounded call; grouped per project client-side (no project filter exists). */
export function useAgents() {
  return useQuery({
    queryKey: queryKeys.agents(),
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/agents", {
          params: { query: { limit: 200 } },
        }),
      ),
  });
}

export function useAgentCounts(): {
  byProject: Map<string, ProjectAgentCounts>;
  isLoading: boolean;
  isError: boolean;
} {
  const { data, isLoading, isError } = useAgents();
  return {
    byProject: agentsByProject(data?.items ?? []),
    isLoading,
    isError,
  };
}

export function useSprints(pid: string) {
  return useQuery({
    queryKey: queryKeys.projectSprints(pid),
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/projects/{project_id}/sprints", {
          params: { path: { project_id: pid }, query: { limit: 100 } },
        }),
      ),
  });
}

export function useMilestones(pid: string) {
  return useQuery({
    queryKey: queryKeys.projectMilestones(pid),
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/projects/{project_id}/milestones", {
          params: { path: { project_id: pid }, query: { limit: 100 } },
        }),
      ),
  });
}

/**
 * A Phase-51 derived artifact. Kinds: roadmap | kanban | dependency_graph |
 * release_plan | risk_analysis | debt_report. `content_md` is the Markdown body.
 * A 404 means the artifact has never been generated for this project — a normal
 * empty state, not an error, so it is not retried.
 */
export function useArtifact(pid: string, kind: string) {
  return useQuery({
    queryKey: queryKeys.projectArtifact(pid, kind),
    retry: false,
    queryFn: async () =>
      unwrap(
        await browserApi().GET(
          "/api/v1/projects/{project_id}/artifacts/{kind}",
          { params: { path: { project_id: pid, kind } } },
        ),
      ),
  });
}

export function useClarifications(pid: string, enabled = true) {
  return useQuery({
    enabled,
    retry: false,
    queryKey: queryKeys.projectClarifications(pid),
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/projects/{project_id}/clarifications", {
          params: { path: { project_id: pid } },
        }),
      ),
  });
}

export function useAnswerClarifications(pid: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (answers: { question_id: string; answer: string }[]) =>
      unwrap(
        await browserApi().POST(
          "/api/v1/projects/{project_id}/clarifications/answers",
          { params: { path: { project_id: pid } }, body: { answers } },
        ),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({
        queryKey: queryKeys.projectClarifications(pid),
      });
    },
  });
}

export function useToolApprovals(pid: string, status = "pending") {
  return useQuery({
    queryKey: [...queryKeys.projectApprovals(pid), status],
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/projects/{project_id}/tool-approvals", {
          params: { path: { project_id: pid }, query: { status, limit: 50 } },
        }),
      ),
  });
}

/**
 * Approve / deny a tool approval. NOTE: the API's resolution body is empty with
 * `extra="forbid"`, so there is no way to attach a comment — §6.1's
 * "Approve/Reject/Comment" is a 2-of-3 until the backend grows a field. The verb
 * is `:deny`, not `:reject`.
 */
export function useResolveApproval(pid: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      approvalId: string;
      action: "approve" | "deny";
    }) => {
      const path = { project_id: pid, approval_id: input.approvalId };
      const result =
        input.action === "approve"
          ? await browserApi().POST(
              "/api/v1/projects/{project_id}/tool-approvals/{approval_id}:approve",
              { params: { path }, body: {} },
            )
          : await browserApi().POST(
              "/api/v1/projects/{project_id}/tool-approvals/{approval_id}:deny",
              { params: { path }, body: {} },
            );
      return unwrap(result);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.approvalsAll() });
      void qc.invalidateQueries({ queryKey: queryKeys.notifications() });
      void qc.invalidateQueries({ queryKey: queryKeys.notificationsUnread() });
    },
  });
}
