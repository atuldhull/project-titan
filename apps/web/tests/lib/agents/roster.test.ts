/**
 * Joining a runtime agent to its roster definition (the §6.5 tier grouping).
 *
 * The join is the interesting part: two different role vocabularies, bridged by
 * a field the API computes. These pin that the bridge is used and that an
 * unjoinable agent still reaches the grid.
 */

import { describe, expect, it } from "vitest";

import type { AgentDefinition, AgentOut } from "@titan/api-client";

import {
  TIER_ORDER,
  definitionFor,
  definitionsById,
  groupByTier,
  tierFor,
  tierLabel,
} from "@/lib/agents/roster";

function agent(over: Partial<AgentOut> = {}): AgentOut {
  return {
    id: "aaaaaaaa-0000-7000-8000-000000000001",
    project_id: null,
    role: "backend",
    roster_id: "eng_backend",
    name: "BE-1",
    status: "idle",
    model_binding: {},
    config: {},
    current_task_id: null,
    created_at: "2026-07-01T00:00:00Z",
    version: 1,
    ...over,
  } as AgentOut;
}

function definition(
  agent_id: string,
  tier: AgentDefinition["tier"],
): AgentDefinition {
  return { agent_id, display_name: agent_id, tier } as AgentDefinition;
}

const DEFS = [
  definition("ceo", 0),
  definition("architect", 1),
  definition("pm_project", 2),
  definition("eng_backend", 3),
  definition("critic", "cross"),
];

describe("definitionFor: the role vocabularies do not match", () => {
  it("joins through roster_id, not role", () => {
    // The whole point: the agent's role is `backend`; its definition's id is
    // `eng_backend`. Joining on `role` would find nothing and every engineer
    // would lose its tier.
    const byId = definitionsById(DEFS);
    const a = agent({ role: "backend", roster_id: "eng_backend" });
    expect(byId.get(a.role)).toBeUndefined();
    expect(definitionFor(a, byId)?.agent_id).toBe("eng_backend");
  });

  it("is null when the API could not map the role", () => {
    // roster_id is null for a role with no roster entry — the map is not total.
    const byId = definitionsById(DEFS);
    expect(definitionFor(agent({ roster_id: null }), byId)).toBeNull();
  });

  it("is null when roster_id names a definition that is not loaded", () => {
    const byId = definitionsById(DEFS);
    expect(definitionFor(agent({ roster_id: "ghost" }), byId)).toBeNull();
  });
});

describe("tierFor", () => {
  it("takes the tier from the joined definition", () => {
    expect(tierFor(agent({ roster_id: "ceo" }), definitionsById(DEFS))).toBe(0);
  });

  it("keeps an unjoinable agent on the grid rather than dropping it", () => {
    // §6.5: the grid never blanks. An agent whose role has no roster entry is
    // still a real agent and must render somewhere.
    expect(tierFor(agent({ roster_id: null }), definitionsById(DEFS))).toBe(
      "cross",
    );
  });
});

describe("groupByTier", () => {
  it("orders sections top-of-org first and drops empty tiers", () => {
    const byId = definitionsById(DEFS);
    const agents = [
      agent({ id: "1", roster_id: "eng_backend" }),
      agent({ id: "2", roster_id: "ceo" }),
      agent({ id: "3", roster_id: "eng_backend" }),
    ];
    const groups = groupByTier(agents, (a) => tierFor(a, byId));
    expect(groups.map((g) => g.tier)).toEqual([0, 3]);
    expect(groups[0].items).toHaveLength(1);
    expect(groups[1].items).toHaveLength(2);
  });

  it("never loses an agent", () => {
    const byId = definitionsById(DEFS);
    const agents = [
      agent({ id: "1", roster_id: "ceo" }),
      agent({ id: "2", roster_id: null }),
      agent({ id: "3", roster_id: "ghost" }),
    ];
    const total = groupByTier(agents, (a) => tierFor(a, byId)).reduce(
      (n, g) => n + g.items.length,
      0,
    );
    expect(total).toBe(agents.length);
  });

  it("returns nothing for no agents", () => {
    expect(groupByTier([], () => 0)).toEqual([]);
  });
});

describe("tier labels", () => {
  it("labels every tier the roster can produce", () => {
    for (const tier of TIER_ORDER) {
      expect(tierLabel(tier)).toMatch(/\S/);
    }
  });

  it("does not claim §6.5's executive/management/engineering/quality carve-up", () => {
    // Those four words describe a hierarchy the roster does not have: tier 3
    // holds engineers AND qa AND security AND docs together.
    const labels = TIER_ORDER.map(tierLabel).join(" ").toLowerCase();
    expect(labels).not.toContain("executive");
    expect(labels).not.toContain("quality");
  });
});
