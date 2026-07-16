"use client";

/**
 * Data + mutations for the Kanban board (Vol 4 §6.3).
 *
 * Fetching is React Query throughout (TITAN-FRT-014). The board's reconciliation
 * state (`board-store.ts`) is a VIEW MODEL layered over the query data, not a
 * second server cache: it holds the optimistic overlay and the seq ordering that
 * decide how a frame, an HTTP body and a drag combine — none of which a query
 * cache can express. The query remains the source of the snapshot.
 */

import { useCallback, useReducer, useRef, type MutableRefObject } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { unwrap } from "@titan/api-client";
import type { components } from "@titan/schemas";

import { browserApi } from "@/lib/query/browser-client";
import { queryKeys } from "@/lib/query/keys";
import { type BoardState, emptyBoard } from "@/lib/kanban/board-store";
import type { WorkflowDefinition } from "@/lib/kanban/workflow";

export type TaskOut = components["schemas"]["TaskOut"];
export type MessageOut = components["schemas"]["MessageOut"];
export type RunOut = components["schemas"]["RunOut"];

/**
 * The task state machine. Effectively immutable — it is compiled into the API —
 * so it is cached hard: refetching it per board mount would be pure waste.
 */
export function useWorkflow() {
  return useQuery<WorkflowDefinition>({
    queryKey: queryKeys.workflow(),
    staleTime: Infinity,
    gcTime: Infinity,
    queryFn: async () => unwrap(await browserApi().GET("/api/v1/workflow", {})),
  });
}

/**
 * The project's dependency graph — the only readable source of task
 * dependencies, since `/tasks/{id}/dependencies` is POST-only.
 */
export function useProjectGraph(pid: string, enabled = true) {
  return useQuery({
    enabled,
    queryKey: queryKeys.projectGraph(pid),
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/projects/{project_id}/graph", {
          params: { path: { project_id: pid } },
        }),
      ),
  });
}

/** One task in full — the Sheet needs `spec`, which the board card lacks. */
export function useTask(pid: string, taskId: string | null) {
  return useQuery({
    enabled: taskId !== null,
    queryKey: queryKeys.task(pid, taskId ?? ""),
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/tasks/{task_id}", {
          params: { path: { task_id: taskId ?? "" } },
        }),
      ),
  });
}

/**
 * A task's message thread — which is also its event history: every transition
 * writes one (`_write_transition_message`). Two things the caller must respect:
 * the log is HOLEY (a system transition with no `message_kind` writes nothing)
 * and LOSSY (many actions share one `kind`, so what happened is `body.action`).
 */
export function useTaskMessages(pid: string, taskId: string | null) {
  return useQuery({
    enabled: taskId !== null,
    queryKey: queryKeys.taskMessages(pid, taskId ?? ""),
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/projects/{project_id}/messages", {
          params: {
            path: { project_id: pid },
            query: { task_id: taskId ?? "", limit: 100 },
          },
        }),
      ),
  });
}

/** A task's run attempts — `/runs` is the one endpoint with a task_id filter. */
export function useTaskRuns(pid: string, taskId: string | null) {
  return useQuery({
    enabled: taskId !== null,
    queryKey: queryKeys.taskRuns(pid, taskId ?? ""),
    queryFn: async () =>
      unwrap(
        await browserApi().GET("/api/v1/runs", {
          params: { query: { task_id: taskId ?? "", limit: 20 } },
        }),
      ),
  });
}

export interface TransitionInput {
  readonly taskId: string;
  readonly action: string;
  readonly actorAgentId?: string;
}

/**
 * Move a task. The ONLY way task state changes — there is no status PATCH.
 *
 * Deliberately has no `onSuccess` invalidation: the move's `task.kanban` frame
 * already invalidates the board (Vol 4 §4.2 — the socket is the canonical "it
 * happened" signal), and the caller settles its own optimistic overlay from the
 * returned task.
 */
export function useTransitionTask() {
  return useMutation({
    mutationFn: async (input: TransitionInput): Promise<TaskOut> => {
      const body: { action: string; actor_agent_id?: string } = {
        action: input.action,
      };
      if (input.actorAgentId !== undefined) {
        body.actor_agent_id = input.actorAgentId;
      }
      return unwrap(
        await browserApi().POST("/api/v1/tasks/{task_id}:transition", {
          params: { path: { task_id: input.taskId } },
          body,
        }),
      );
    },
  });
}

/**
 * Hold the board's reconciliation state.
 *
 * The state lives in a ref, not `useState`, on purpose. Applying a frame yields
 * both a new state AND effects (a conflict to toast, an agent move to animate),
 * and computing those inside a `setState` updater would fire them twice under
 * StrictMode, which double-invokes updaters to smoke out exactly this kind of
 * impurity. Frames arrive in event handlers — outside render — so the ref is
 * always current by the time we re-render, and the reducer stays pure and
 * directly testable.
 *
 * ONE ref, and `commit` is its ONLY writer. Callers read `stateRef.current` and
 * hand the next state to `commit`; they must never assign it themselves. That is
 * not a style rule — `commit` skips the re-render when handed the object the ref
 * already holds (a reducer returning its input means nothing happened), so a
 * caller that assigned first and committed second would silently render nothing.
 */
export function useBoardState(projectId: string): {
  stateRef: MutableRefObject<BoardState>;
  commit: (next: BoardState) => void;
} {
  const ref = useRef<BoardState>(emptyBoard(projectId));
  const [, forceRender] = useReducer((n: number) => n + 1, 0);

  // A different project is a different board AND a different seq sequence —
  // carrying either over would be wrong. Reassign during render (cheap, no
  // effect needed): the ref is not rendered state, so this is not a side effect.
  if (ref.current.projectId !== projectId) {
    ref.current = emptyBoard(projectId);
  }

  const commit = useCallback((next: BoardState) => {
    if (next === ref.current) {
      return; // reducer returned the same object: nothing changed, don't render
    }
    ref.current = next;
    forceRender();
  }, []);

  return { stateRef: ref, commit };
}
