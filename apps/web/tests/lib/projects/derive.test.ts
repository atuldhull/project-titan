import { describe, expect, it } from "vitest";

import type { components } from "@titan/schemas";

import {
  agentsByProject,
  boardStats,
  countWorking,
} from "@/lib/projects/derive";

type Board = components["schemas"]["Board"];
type AgentOut = components["schemas"]["AgentOut"];

function board(counts: Record<string, number>): Board {
  return {
    project_id: "p1",
    columns: Object.entries(counts).map(([status, n]) => ({
      status,
      tasks: Array.from({ length: n }, (_, i) => ({
        id: `${status}-${i}`,
      })) as Board["columns"][number]["tasks"],
    })),
  };
}

function agent(over: Partial<AgentOut>): AgentOut {
  return {
    id: "a",
    name: "n",
    role: "backend",
    status: "idle",
    project_id: "p1",
    current_task_id: null,
    config: {},
    model_binding: {},
    created_at: "2026-07-16T00:00:00Z",
    version: 1,
    ...over,
  } as AgentOut;
}

describe("boardStats (progress is DERIVED — ProjectOut has no progress field)", () => {
  it("counts done + merged as completed", () => {
    const stats = boardStats(board({ done: 2, merged: 1, in_progress: 1 }));
    expect(stats.completed).toBe(3);
    expect(stats.total).toBe(4);
    expect(stats.progressPct).toBe(75);
  });

  it("excludes cancelled work from the denominator", () => {
    // 1 done of 1 real task, plus 3 cancelled → 100%, not 25%.
    const stats = boardStats(board({ done: 1, cancelled: 3 }));
    expect(stats.total).toBe(1);
    expect(stats.progressPct).toBe(100);
  });

  it("counts blocked, failed and escalated as blocked", () => {
    const stats = boardStats(
      board({ blocked: 2, failed: 1, escalated: 1, ready: 5 }),
    );
    expect(stats.blocked).toBe(4);
  });

  it("returns null progress for an empty board rather than a fake 0%", () => {
    expect(boardStats(board({})).progressPct).toBeNull();
    expect(boardStats(undefined).progressPct).toBeNull();
    expect(boardStats(undefined).total).toBe(0);
  });

  it("rounds to a whole percent", () => {
    expect(boardStats(board({ done: 1, ready: 2 })).progressPct).toBe(33);
  });
});

describe("agentsByProject (/agents has no project_id filter)", () => {
  it("groups by project and counts busy as working", () => {
    const byPid = agentsByProject([
      agent({ id: "1", project_id: "p1", status: "busy" }),
      agent({ id: "2", project_id: "p1", status: "idle" }),
      agent({ id: "3", project_id: "p2", status: "busy" }),
    ]);
    expect(byPid.get("p1")).toEqual({ total: 2, working: 1 });
    expect(byPid.get("p2")).toEqual({ total: 1, working: 1 });
  });

  it("ignores org-level agents with no project", () => {
    const byPid = agentsByProject([agent({ id: "1", project_id: null })]);
    expect(byPid.size).toBe(0);
  });

  it("counts only busy agents as working (paused/error are not)", () => {
    expect(
      countWorking([
        agent({ status: "busy" }),
        agent({ status: "paused" }),
        agent({ status: "error" }),
        agent({ status: "idle" }),
      ]),
    ).toBe(1);
  });
});
