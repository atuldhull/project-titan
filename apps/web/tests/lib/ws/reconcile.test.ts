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

  it("maps notification frames to both notification keys", () => {
    expect(
      queryKeysForFrame(domainFrame("notification.created", null)),
    ).toEqual([queryKeys.notifications(), queryKeys.notificationsUnread()]);
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
