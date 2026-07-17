import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import type { AgentDefinition, AgentOut } from "@titan/api-client";

import type { ProjectRun } from "@/lib/agents/types";

import { AgentCard } from "./agent-card";
import { StateChip } from "./state-chip";
import { agentView } from "@/lib/agents/view";
import { definitionsById } from "@/lib/agents/roster";

/**
 * The §6.5 card in the states it can really be in (TITAN-FRT-002).
 *
 * Stories cover the CARD, not the grid: the grid's four states are query
 * states, and driving them needs a QueryClient + a socket the story has no
 * business standing up. The card is the surface where the derivation shows.
 *
 * The extra stories past the usual four are the honest ones — a paused agent, a
 * configured-but-never-run model chip, and an agent with no roster contract are
 * all real conditions that look like bugs until you know they aren't.
 */
const meta: Meta<typeof AgentCard> = {
  title: "Agents/AgentCard",
  component: AgentCard,
};
export default meta;
type Story = StoryObj<typeof AgentCard>;

const DEFINITION = {
  agent_id: "eng_backend",
  display_name: "Backend Engineer",
  tier: 3,
  supervisor: "pm_project",
  kpis: ["diff coverage >= 80%"],
  tool_grants: [],
  memory_scopes: { read: [], write: [] },
  model_policy: {
    tier: "standard",
    temperature: 0.2,
    max_output_tokens: 4096,
    structured_output_schema: null,
  },
  authority: { decides_alone: [], must_escalate: [], veto: false },
  failure_policy: {
    mode: "default",
    escalates_to_human: false,
    watchdog_restart: false,
    skip_retries_on: [],
    notes: null,
  },
  system_prompt_template: "",
} as AgentDefinition;

const BY_ID = definitionsById([DEFINITION]);

const AGENT: AgentOut = {
  id: "a1",
  project_id: "p1",
  role: "backend",
  roster_id: "eng_backend",
  name: "BE-1",
  status: "idle",
  model_binding: { provider: "anthropic", model: "claude-opus-4-8" },
  config: {},
  current_task_id: null,
  created_at: "2026-07-01T00:00:00Z",
  version: 1,
} as AgentOut;

const NOW = Date.parse("2026-07-01T00:10:00Z");

function run(over: Partial<ProjectRun>): ProjectRun {
  return {
    id: "r1",
    task_id: "t1",
    agent_id: "a1",
    // Stamped by the fetch, not the API: RunOut has no project_id.
    project_id: "p1",
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
    created_at: "2026-07-01T00:00:00Z",
    version: 1,
    ...over,
  } as ProjectRun;
}

/** Working: a live run, so a real task, model, elapsed time. */
export const Working: Story = {
  args: {
    view: agentView(
      AGENT,
      [
        run({
          status: "running",
          provider: "ollama",
          model: "qwen2.5-coder:7b",
          started_at: "2026-07-01T00:07:50Z",
        }),
      ],
      BY_ID,
      NOW,
    ),
  },
};

/** Idle with history: the throughput chip is the LAST COMPLETED run's average. */
export const Idle: Story = {
  args: {
    view: agentView(
      AGENT,
      [
        run({
          status: "succeeded",
          provider: "ollama",
          model: "qwen2.5-coder:7b",
          tokens_out: 1280,
          started_at: "2026-07-01T00:00:00Z",
          finished_at: "2026-07-01T00:00:02Z",
        }),
      ],
      BY_ID,
      NOW,
    ),
  },
};

/** Error: the run's own error text, and the card links to it. */
export const Error: Story = {
  args: {
    view: agentView(
      AGENT,
      [run({ status: "failed", error: "LLM timeout after 3 attempts" })],
      BY_ID,
      NOW,
    ),
  },
};

/** Paused — the only status a human ever writes to the agents table. */
export const Paused: Story = {
  args: { view: agentView({ ...AGENT, status: "paused" }, [], BY_ID, NOW) },
};

/**
 * Never run. The model chip shows the CONFIGURED binding and says so — §6.5
 * asks for the model "in use", and until a run exists there isn't one.
 */
export const NeverRun: Story = {
  args: { view: agentView(AGENT, [], BY_ID, NOW) },
};

/**
 * An agent whose role has no roster contract (ROLE_TO_ROSTER is not total). It
 * keeps its card — §6.5: the grid never blanks — and simply has no tier badge.
 */
export const NoRosterContract: Story = {
  args: { view: agentView({ ...AGENT, roster_id: null }, [], new Map(), NOW) },
};

export const Chips: StoryObj<typeof StateChip> = {
  render: () => (
    <div className="flex gap-2">
      <StateChip state="working" />
      <StateChip state="idle" />
      <StateChip state="paused" />
      <StateChip state="error" />
    </div>
  ),
};
