/**
 * The grid's view-model: the join, the filters, and the roll-up.
 */

import { describe, expect, it } from "vitest";

import type { AgentDefinition, AgentOut } from "@titan/api-client";

import type { ProjectRun } from "@/lib/agents/types";

import {
  agentViews,
  filterViews,
  groupViews,
  needsAllProjects,
  projectIdsOf,
  rolesPresent,
  rollupOf,
  runProjectIds,
} from "@/lib/agents/view";

const PID = "pppppppp-0000-7000-8000-000000000001";

function agent(over: Partial<AgentOut> = {}): AgentOut {
  return {
    id: "a1",
    project_id: PID,
    role: "backend",
    roster_id: "eng_backend",
    name: "BE-1",
    status: "idle",
    model_binding: { provider: "anthropic", model: "claude-opus-4-8" },
    config: {},
    current_task_id: null,
    created_at: "2026-07-01T00:00:00Z",
    version: 1,
    ...over,
  } as AgentOut;
}

let seq = 0;
function run(over: Partial<ProjectRun> = {}): ProjectRun {
  seq += 1;
  return {
    id: `run-${seq}`,
    task_id: "t1",
    agent_id: "a1",
    project_id: PID,
    attempt: 1,
    status: "queued",
    input: {},
    output_summary: null,
    error: null,
    provider: null,
    model: null,
    tokens_in: 0,
    tokens_out: 0,
    cost_usd: "0",
    started_at: null,
    finished_at: null,
    created_at: `2026-07-01T00:00:${String(seq).padStart(2, "0")}Z`,
    version: 1,
    ...over,
  } as ProjectRun;
}

const DEFS = [
  { agent_id: "eng_backend", display_name: "Backend", tier: 3 },
  { agent_id: "ceo", display_name: "CEO", tier: 0 },
] as AgentDefinition[];

const NOW = Date.parse("2026-07-01T01:00:00Z");

describe("agentViews", () => {
  it("joins each agent to its runs and its definition", () => {
    const agents = [
      agent({ id: "a1" }),
      agent({ id: "a2", roster_id: "ceo", role: "ceo" }),
    ];
    const runs = [run({ agent_id: "a1", status: "running", task_id: "t-42" })];
    const views = agentViews(agents, DEFS, runs, NOW);

    expect(views[0].state).toBe("working");
    expect(views[0].currentTaskId).toBe("t-42");
    expect(views[0].tier).toBe(3);
    expect(views[1].state).toBe("idle");
    expect(views[1].tier).toBe(0);
  });

  it("does not attribute one agent's runs to another", () => {
    const agents = [agent({ id: "a1" }), agent({ id: "a2" })];
    const runs = [run({ agent_id: "a1", status: "running" })];
    const views = agentViews(agents, DEFS, runs, NOW);
    expect(views.map((v) => v.state)).toEqual(["working", "idle"]);
  });

  it("renders every agent even with no runs and no definitions loaded", () => {
    // §6.5: the grid never blanks.
    const views = agentViews(
      [agent(), agent({ id: "a2", roster_id: null })],
      [],
      [],
      NOW,
    );
    expect(views).toHaveLength(2);
    expect(views.every((v) => v.definition === null)).toBe(true);
  });

  it("points logRunId at the failure when there is one", () => {
    // TITAN-FRT-027's "one-click path to its logs" — from an error card, the
    // useful run is the one that failed.
    const failed = run({
      agent_id: "a1",
      status: "failed",
      error: "LLM timeout",
    });
    const views = agentViews([agent()], DEFS, [failed], NOW);
    expect(views[0].failure?.error).toBe("LLM timeout");
    expect(views[0].logRunId).toBe(failed.id);
  });

  it("points logRunId at the newest run when nothing failed", () => {
    const older = run({ agent_id: "a1", status: "succeeded" });
    const newest = run({ agent_id: "a1", status: "running" });
    const views = agentViews([agent()], DEFS, [older, newest], NOW);
    expect(views[0].logRunId).toBe(newest.id);
  });

  it("has no run to link to when the agent has never run", () => {
    expect(agentViews([agent()], DEFS, [], NOW)[0].logRunId).toBeNull();
  });
});

describe("filterViews", () => {
  const views = agentViews(
    [
      agent({ id: "a1", role: "backend" }),
      agent({ id: "a2", role: "qa" }),
      agent({ id: "a3", role: "qa", status: "paused" }),
    ],
    DEFS,
    [run({ agent_id: "a1", status: "running" })],
    NOW,
  );

  it("filters by role", () => {
    expect(filterViews(views, { role: "qa" }).map((v) => v.agent.id)).toEqual([
      "a2",
      "a3",
    ]);
  });

  it("filters by state", () => {
    expect(
      filterViews(views, { state: "working" }).map((v) => v.agent.id),
    ).toEqual(["a1"]);
  });

  it("composes both", () => {
    expect(
      filterViews(views, { role: "qa", state: "paused" }).map(
        (v) => v.agent.id,
      ),
    ).toEqual(["a3"]);
  });

  it("returns everything when nothing is selected", () => {
    expect(filterViews(views, {})).toHaveLength(3);
    expect(filterViews(views, { role: null, state: null })).toHaveLength(3);
  });
});

describe("rollupOf", () => {
  it("counts the whole org, so a filter cannot rewrite the header", () => {
    // The header states a fact about the organization. Narrowing to one role
    // must not silently restate it as a fact about that role.
    const views = agentViews(
      [agent({ id: "a1", role: "backend" }), agent({ id: "a2", role: "qa" })],
      DEFS,
      [run({ agent_id: "a1", status: "running" })],
      NOW,
    );
    expect(rollupOf(views)).toEqual({
      working: 1,
      idle: 1,
      error: 0,
      paused: 0,
    });
    // The caller passes the UNFILTERED views; this asserts the contract by
    // showing the filtered set would say something different.
    expect(rollupOf(filterViews(views, { role: "qa" }))).toEqual({
      working: 0,
      idle: 1,
      error: 0,
      paused: 0,
    });
  });
});

describe("groupViews", () => {
  it("sections by tier, top of org first", () => {
    const views = agentViews(
      [agent({ id: "a1" }), agent({ id: "a2", roster_id: "ceo", role: "ceo" })],
      DEFS,
      [],
      NOW,
    );
    expect(groupViews(views).map((g) => g.tier)).toEqual([0, 3]);
  });
});

describe("rolesPresent / projectIdsOf", () => {
  it("lists the distinct roles for the filter menu", () => {
    const views = agentViews(
      [
        agent({ id: "a1", role: "qa" }),
        agent({ id: "a2", role: "backend" }),
        agent({ id: "a3", role: "qa" }),
      ],
      DEFS,
      [],
      NOW,
    );
    expect(rolesPresent(views)).toEqual(["backend", "qa"]);
  });

  it("collects each named project once", () => {
    const agents = [
      agent({ id: "a1", project_id: "p1" }),
      agent({ id: "a2", project_id: "p1" }),
      agent({ id: "a3", project_id: null }),
      agent({ id: "a4", project_id: "p2" }),
    ];
    expect(projectIdsOf(agents)).toEqual(["p1", "p2"]);
  });

  it("asks for no projects when there are no agents", () => {
    expect(projectIdsOf([])).toEqual([]);
  });
});

describe("runProjectIds: an org-level agent is not a run-less template", () => {
  it("looks in EVERY project once any agent is org-level", () => {
    // The bug this pins: project_id null does not mean "no work". Nothing ties
    // a run's project to its agent's — a run belongs to its TASK's project —
    // so an org-level agent's runs are found only by asking every project.
    // Deriving the fetch list from agents alone rendered them permanently idle.
    const agents = [agent({ id: "a1", project_id: null })];
    expect(needsAllProjects(agents)).toBe(true);
    expect(runProjectIds(agents, ["p1", "p2"])).toEqual(["p1", "p2"]);
  });

  it("stays narrow when every agent is project-bound", () => {
    // No org-level agent -> no reason to fan out over projects with no agents.
    const agents = [agent({ id: "a1", project_id: "p1" })];
    expect(needsAllProjects(agents)).toBe(false);
    expect(runProjectIds(agents, ["p1", "p2", "p3"])).toEqual(["p1"]);
  });

  it("unions the agents' own projects with the rest", () => {
    const agents = [
      agent({ id: "a1", project_id: "p1" }),
      agent({ id: "a2", project_id: null }),
    ];
    expect(runProjectIds(agents, ["p2"]).sort()).toEqual(["p1", "p2"]);
  });
});

describe("runProjectId: where the agent's work actually is", () => {
  it("comes from the RUN, so an org-level agent still links to its task", () => {
    // agent.project_id is null; the run's is not. A task link built from the
    // agent's own column would be missing on exactly the busiest cards.
    const orgAgent = agent({ id: "a1", project_id: null });
    const views = agentViews(
      [orgAgent],
      DEFS,
      [
        run({
          agent_id: "a1",
          status: "running",
          project_id: "p9",
          task_id: "t9",
        }),
      ],
      NOW,
    );
    expect(views[0].runProjectId).toBe("p9");
    expect(views[0].currentTaskId).toBe("t9");
  });

  it("falls back to the agent's own project when it has never run", () => {
    expect(
      agentViews([agent({ project_id: "p1" })], DEFS, [], NOW)[0].runProjectId,
    ).toBe("p1");
  });
});
