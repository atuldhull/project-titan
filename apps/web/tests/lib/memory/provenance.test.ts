/**
 * TITAN-FRT-034's provenance chain and usage history.
 */
import { describe, expect, it } from "vitest";

import type { MemoryRecordDetail } from "@titan/api-client";

import {
  provenanceComplete,
  provenanceOf,
  readCount,
  usageHistory,
} from "@/lib/memory/provenance";

const PID = "11111111-1111-7111-8111-111111111111";
const TASK = "22222222-2222-7222-8222-222222222222";
const NEWER = "33333333-3333-7333-8333-333333333333";

function detail(
  over: Record<string, unknown> = {},
  ledger: unknown[] = [],
): MemoryRecordDetail {
  return {
    record: {
      id: "44444444-4444-7444-8444-444444444444",
      kind: "semantic",
      scope: "org",
      status: "active",
      title: "JWT refresh rotation",
      body_md: "…",
      metadata: {},
      importance: 0.5,
      decay_score: 1,
      access_count: 0,
      last_accessed_at: null,
      demoted: false,
      source_agent: "eng_backend",
      source_task_id: null,
      project_id: null,
      created_at: "2026-07-01T00:00:00Z",
      superseded_by: null,
      ...over,
    },
    chunks: [],
    ledger,
  } as MemoryRecordDetail;
}

describe("provenanceOf: the agent is named, never linked", () => {
  it("renders source_agent as un-linked text with a reason", () => {
    // `/agents/[id]` resolves by the agents-table uuid; source_agent is a name.
    // Any href would 404, so it is always text.
    const agent = provenanceOf(detail()).find((l) => l.kind === "agent");
    expect(agent?.label).toBe("eng_backend");
    expect(agent?.href).toBeNull();
    expect(agent?.reason).toBeTruthy();
  });

  it("does the same for an internal writer name with a dot", () => {
    const agent = provenanceOf(
      detail({ source_agent: "agent.memory_agent" }),
    ).find((l) => l.kind === "agent");
    expect(agent?.href).toBeNull();
  });
});

describe("provenanceOf: the task is the one real link", () => {
  it("links a task when the record also has a project", () => {
    const task = provenanceOf(
      detail({ source_task_id: TASK, project_id: PID }),
    ).find((l) => l.kind === "task");
    expect(task?.href).toBe(`/projects/${PID}/board?task=${TASK}`);
  });

  it("does NOT link a task on an org record with no project", () => {
    // The board route is project-scoped; there is no project to open it in.
    const task = provenanceOf(
      detail({ source_task_id: TASK, project_id: null }),
    ).find((l) => l.kind === "task");
    expect(task?.href).toBeNull();
    expect(task?.reason).toBeTruthy();
  });

  it("links the project when present", () => {
    const project = provenanceOf(detail({ project_id: PID })).find(
      (l) => l.kind === "project",
    );
    expect(project?.href).toBe(`/projects/${PID}`);
  });

  it("links a superseding revision", () => {
    const rev = provenanceOf(detail({ superseded_by: NEWER })).find(
      (l) => l.kind === "record",
    );
    expect(rev?.href).toBe(`/memory?record=${NEWER}`);
  });
});

describe("provenanceComplete: FRT-034's three parts", () => {
  it("is complete only with agent, timestamp AND a source entity", () => {
    expect(provenanceComplete(detail({ source_task_id: TASK }))).toBe(true);
    // An agent and a time but no source entity is provenance without the entity.
    expect(provenanceComplete(detail({ source_task_id: null }))).toBe(false);
  });
});

describe("usageHistory", () => {
  const ledger = [
    {
      at: "2026-07-02T00:00:00Z",
      action: "write",
      actor_agent: "eng_backend",
      task_id: null,
    },
    {
      at: "2026-07-03T00:00:00Z",
      action: "read",
      actor_agent: "qa",
      task_id: TASK,
    },
    {
      at: "2026-07-04T00:00:00Z",
      action: "read",
      actor_agent: "pm",
      task_id: null,
    },
  ];

  it("maps the append-only ledger to events", () => {
    const events = usageHistory(detail({}, ledger));
    expect(events).toHaveLength(3);
    expect(events[1]).toMatchObject({
      action: "read",
      actor: "qa",
      taskId: TASK,
    });
  });

  it("counts only reads for the retrieval tally", () => {
    // §6.11's "which retrievals used it" is the `read` action specifically.
    expect(readCount(detail({}, ledger))).toBe(2);
  });

  it("is an empty list, not a throw, when the record was never used", () => {
    expect(usageHistory(detail())).toEqual([]);
    expect(readCount(detail())).toBe(0);
  });
});
