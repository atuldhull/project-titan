import type { QueryKey } from "@tanstack/react-query";
import type { DomainFrameType, ServerFrame } from "@titan/ws-protocol";

import { queryKeys } from "@/lib/query/keys";

/**
 * Maps a realtime frame to the React Query keys whose cached data it affects.
 * Invalidation is driven by these WS confirmation events, not the HTTP response
 * (Vol 4 §4.2) — the socket is the canonical "it happened" signal. Keys are
 * PREFIXES so every filtered variant (e.g. all task filters) is refetched.
 */
export function queryKeysForFrame(
  frame: ServerFrame & { type: DomainFrameType },
): QueryKey[] {
  const pid = frame.project_id;
  switch (frame.type) {
    case "task.created":
    case "task.updated":
    case "task.kanban":
      return pid
        ? [queryKeys.projectTasksAll(pid), queryKeys.project(pid)]
        : [];
    case "run.started":
    case "run.finished":
    case "run.output.delta":
      return pid ? [queryKeys.projectRuns(pid)] : [];
    case "message.created":
      return pid ? [queryKeys.projectMessages(pid)] : [];
    case "pipeline.updated":
      return pid ? [queryKeys.projectDeployments(pid)] : [];
    case "agent.status":
      return [queryKeys.agents()];
    case "notification.created":
      return [queryKeys.notifications(), queryKeys.notificationsUnread()];
    case "activity.created":
      return [queryKeys.orgActivity()];
    case "decision.recorded":
    case "sprint.updated":
    case "handoff.evaluated":
    case "clarification.updated":
      return pid ? [queryKeys.project(pid)] : [];
    case "sys.notice":
      return [];
    default:
      return [];
  }
}

/**
 * The full set of keys to refetch for a project after a seq gap or reconnect
 * (TITAN-FRT-019) — a broad, safe reconciliation that converges the caches to
 * the server with no stale or duplicated items.
 */
export function queryKeysForProject(pid: string): QueryKey[] {
  return [
    queryKeys.projectTasksAll(pid),
    queryKeys.projectRuns(pid),
    queryKeys.projectMessages(pid),
    queryKeys.projectDeployments(pid),
    queryKeys.project(pid),
  ];
}
