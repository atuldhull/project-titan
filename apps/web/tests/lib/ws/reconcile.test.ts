import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import {
  PROTOCOL_VERSION,
  type DomainFrameType,
  type ServerFrame,
} from "@titan/ws-protocol";

import { queryKeys } from "@/lib/query/keys";
import { queryKeysForFrame, queryKeysForProject } from "@/lib/ws/reconcile";

function domainFrame(
  type: DomainFrameType,
  projectId: string | null,
): ServerFrame & { type: DomainFrameType } {
  return {
    v: PROTOCOL_VERSION,
    type,
    seq: 1,
    ts: "2026-07-16T00:00:00Z",
    project_id: projectId,
    payload: {},
  };
}

describe("queryKeysForFrame", () => {
  it("maps task frames to the project's task + project keys", () => {
    expect(queryKeysForFrame(domainFrame("task.updated", "p1"))).toEqual([
      queryKeys.projectTasksAll("p1"),
      queryKeys.project("p1"),
    ]);
  });

  it("maps notification frames to the notification keys AND approvals", () => {
    // There is no approval-specific WS frame: a pending tool approval reaches the
    // UI as an approval/critical notification. So notification.created is also
    // what refreshes the Overview's attention queue — this is what makes "a new
    // approval appears within 2 s" real rather than poll-dependent.
    expect(
      queryKeysForFrame(domainFrame("notification.created", null)),
    ).toEqual([
      queryKeys.notifications(),
      queryKeys.notificationsUnread(),
      queryKeys.approvalsAll(),
    ]);
  });

  it("maps agent.status to the agents key", () => {
    expect(queryKeysForFrame(domainFrame("agent.status", null))).toEqual([
      queryKeys.agents(),
    ]);
  });

  it("maps run frames to the project's runs key", () => {
    expect(queryKeysForFrame(domainFrame("run.finished", "p1"))).toEqual([
      queryKeys.projectRuns("p1"),
    ]);
  });

  it("returns nothing for a project-scoped frame without a project", () => {
    expect(queryKeysForFrame(domainFrame("task.updated", null))).toEqual([]);
  });
});

describe("queryKeysForProject", () => {
  it("returns the full set to refetch after a gap/reconnect", () => {
    const keys = queryKeysForProject("p1");
    expect(keys).toContainEqual(queryKeys.projectTasksAll("p1"));
    expect(keys).toContainEqual(queryKeys.projectRuns("p1"));
    expect(keys).toContainEqual(queryKeys.project("p1"));
  });
});

describe("board invalidation (Phase 59)", () => {
  // The board query must refetch when a task moves. It does — but only via the
  // PREFIX relationship: queryKeys.project(pid) is ["project", pid], which
  // partial-matches ["project", pid, "board"]. That is load-bearing and entirely
  // implicit, so pin it against a real QueryClient rather than by reading the
  // key list: projectTasksAll(pid) does NOT match the board, so if project(pid)
  // were ever dropped from the task case the board would silently go static.
  it("a task.kanban frame invalidates the board query", async () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    });
    let fetches = 0;
    await qc.fetchQuery({
      queryKey: queryKeys.projectBoard("p1"),
      queryFn: () => {
        fetches += 1;
        return Promise.resolve({ project_id: "p1", columns: [] });
      },
    });
    expect(fetches).toBe(1);
    expect(qc.getQueryState(queryKeys.projectBoard("p1"))?.isInvalidated).toBe(
      false,
    );

    for (const queryKey of queryKeysForFrame(
      domainFrame("task.kanban", "p1"),
    )) {
      await qc.invalidateQueries({ queryKey });
    }

    expect(qc.getQueryState(queryKeys.projectBoard("p1"))?.isInvalidated).toBe(
      true,
    );
  });

  it("a reconnect reconciliation invalidates the board query", async () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    });
    await qc.fetchQuery({
      queryKey: queryKeys.projectBoard("p1"),
      queryFn: () => Promise.resolve({ project_id: "p1", columns: [] }),
    });
    for (const queryKey of queryKeysForProject("p1")) {
      await qc.invalidateQueries({ queryKey });
    }
    expect(qc.getQueryState(queryKeys.projectBoard("p1"))?.isInvalidated).toBe(
      true,
    );
  });

  it("another project's task frame leaves our board alone", async () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    });
    await qc.fetchQuery({
      queryKey: queryKeys.projectBoard("p1"),
      queryFn: () => Promise.resolve({ project_id: "p1", columns: [] }),
    });
    for (const queryKey of queryKeysForFrame(
      domainFrame("task.kanban", "p2"),
    )) {
      await qc.invalidateQueries({ queryKey });
    }
    expect(qc.getQueryState(queryKeys.projectBoard("p1"))?.isInvalidated).toBe(
      false,
    );
  });
});
