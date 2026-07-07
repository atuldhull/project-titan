// The AgentDefinition shape served by GET /api/v1/agent-definitions (Phase 26,
// Vol 2 §2.1). Declared locally, mirroring the API — the generated @titan/schemas
// data layer is wired into the frontend with the dashboard build-out (M6, Phase 57).

export type AgentTier = 0 | 1 | 2 | 3 | "cross";
export type ModelTier = "reasoning" | "standard" | "fast";
export type FailMode = "default" | "fail_open" | "fail_closed";

export type Escalation = {
  decision: string;
  to: string[];
  co_sign: boolean;
};

export type AgentDefinition = {
  agent_id: string;
  display_name: string;
  tier: AgentTier;
  supervisor: string;
  system_prompt_template: string;
  authority: {
    decides_alone: string[];
    must_escalate: Escalation[];
    veto: boolean;
  };
  memory_scopes: {
    read: string[];
    write: string[];
  };
  tool_grants: string[];
  model_policy: {
    tier: ModelTier;
    temperature: number;
    max_output_tokens: number;
    structured_output_schema: string | null;
  };
  kpis: string[];
  failure_policy: {
    mode: FailMode;
    escalates_to_human: boolean;
    watchdog_restart: boolean;
    skip_retries_on: string[];
    notes: string | null;
  };
};

// Ordered tiers for the roster's section grouping (top-of-org first).
export const TIER_ORDER: AgentTier[] = [0, 1, 2, 3, "cross"];

export const TIER_LABEL: Record<string, string> = {
  "0": "Tier 0 · Vision & Release",
  "1": "Tier 1 · Planning & Design",
  "2": "Tier 2 · Coordination",
  "3": "Tier 3 · Builders & Gatekeepers",
  cross: "Cross-cutting",
};

export const MODEL_TIER_VARIANT: Record<
  ModelTier,
  "default" | "secondary" | "outline"
> = {
  reasoning: "default",
  standard: "secondary",
  fast: "outline",
};
