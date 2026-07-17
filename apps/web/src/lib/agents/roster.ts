/**
 * Joining a runtime agent to its declarative contract (Vol 4 §6.5's tier
 * grouping).
 *
 * §6.5 groups the grid by "hierarchy tier (executive / management / engineering
 * / quality)". Those four words exist nowhere in the system. The real
 * vocabulary is `AgentTier = 0 | 1 | 2 | 3 | "cross"`, and it lives on the YAML
 * roster (`AgentDefinition`), not on the agents table — so a runtime agent has
 * no tier of its own and must be joined to its definition to get one.
 *
 * That join is not on `role`: the agents table and the roster use DIFFERENT
 * vocabularies (`backend` here is `eng_backend` there; `pm` is `pm_product`).
 * The bridge is `AgentOut.roster_id`, which the API computes from its own
 * `ROLE_TO_ROSTER` map — published precisely so this file does not have to
 * re-declare that table and drift from it.
 */

import type { AgentDefinition, AgentOut } from "@titan/api-client";

import type { AgentTier } from "./types";

/** Ordered tiers, top of the org first — the section order of the grid. */
export const TIER_ORDER: readonly AgentTier[] = [0, 1, 2, 3, "cross"];

/**
 * Honest tier labels.
 *
 * NOT §6.5's executive/management/engineering/quality: the roster's tiers do
 * not carve up that way (tier 3 holds the engineers AND qa AND security AND
 * docs; `cross` holds critic/memory/research/reviewer). Naming a tier
 * "quality" would assert a grouping the roster never makes.
 */
export const TIER_LABEL: Record<string, string> = {
  "0": "Tier 0 · Vision & Release",
  "1": "Tier 1 · Planning & Design",
  "2": "Tier 2 · Coordination",
  "3": "Tier 3 · Builders & Gatekeepers",
  cross: "Cross-cutting",
};

export function tierLabel(tier: AgentTier): string {
  return TIER_LABEL[String(tier)] ?? `Tier ${String(tier)}`;
}

/** Definitions keyed by their roster id (`agent_id`). */
export function definitionsById(
  definitions: readonly AgentDefinition[],
): Map<string, AgentDefinition> {
  return new Map(definitions.map((d) => [d.agent_id, d]));
}

/**
 * The definition behind a runtime agent, or null when its role has no roster
 * entry.
 *
 * Null is a real outcome, not a defensive branch: `ROLE_TO_ROSTER` is not total
 * — the roster's `orchestrator` and `critic` have no agents-table role, and an
 * agent row could carry any role string (the column is free text; only the
 * CREATE dto constrains it).
 */
export function definitionFor(
  agent: AgentOut,
  byId: ReadonlyMap<string, AgentDefinition>,
): AgentDefinition | null {
  return agent.roster_id != null ? (byId.get(agent.roster_id) ?? null) : null;
}

/**
 * The tier an agent belongs to.
 *
 * `cross` is the honest home for an agent whose definition cannot be resolved:
 * the roster itself uses that bucket for "does not sit on the hierarchy", which
 * is exactly true of an unmappable role — and it keeps the agent ON the grid.
 * §6.5 is explicit that the grid never blanks, so a card must never be dropped
 * for want of a tier.
 */
export function tierFor(
  agent: AgentOut,
  byId: ReadonlyMap<string, AgentDefinition>,
): AgentTier {
  return definitionFor(agent, byId)?.tier ?? "cross";
}

export interface TierGroup<T> {
  readonly tier: AgentTier;
  readonly label: string;
  readonly items: readonly T[];
}

/**
 * Group items into the roster's tier sections, dropping empty ones.
 *
 * Generic over the item so the grid can group its joined view-models rather
 * than raw agents, and so this stays pure/testable while the cards do not.
 */
export function groupByTier<T>(
  items: readonly T[],
  tierOf: (item: T) => AgentTier,
): TierGroup<T>[] {
  const groups: TierGroup<T>[] = [];
  for (const tier of TIER_ORDER) {
    const members = items.filter((item) => tierOf(item) === tier);
    if (members.length > 0) {
      groups.push({ tier, label: tierLabel(tier), items: members });
    }
  }
  return groups;
}
