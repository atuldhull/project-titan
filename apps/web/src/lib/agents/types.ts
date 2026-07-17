/**
 * Agent types shared by the feature's lib and its components.
 *
 * `AgentDefinition` used to be hand-mirrored in `components/agents/types.ts`
 * because the generated data layer did not exist yet (its header said so). It
 * does now, so the contract types come from `@titan/api-client` and only the
 * things the API cannot express — a display variant, an ordering — live here.
 */

import type { AgentDefinition, RunOut } from "@titan/api-client";

/**
 * A run, plus the project it was fetched from.
 *
 * `RunOut` has no `project_id`: a run reaches its project only through its task,
 * and the SELECT does not join. But the FETCH knows — the route is
 * `/projects/{pid}/runs` — so the data layer stamps it on the way through.
 *
 * This is what lets a card link to a task even for an ORG-LEVEL agent, whose own
 * `project_id` is null while its work is in a perfectly real project.
 */
export type ProjectRun = RunOut & { readonly project_id: string };

/** The roster's real tier vocabulary. NOT §6.5's executive/management/
 *  engineering/quality, which exist nowhere in the system. */
export type AgentTier = AgentDefinition["tier"];
export type ModelTier = AgentDefinition["model_policy"]["tier"];
export type FailMode = AgentDefinition["failure_policy"]["mode"];

export const MODEL_TIER_VARIANT: Record<
  ModelTier,
  "default" | "secondary" | "outline"
> = {
  reasoning: "default",
  standard: "secondary",
  fast: "outline",
};
