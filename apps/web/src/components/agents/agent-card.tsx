"use client";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

import { MODEL_TIER_VARIANT, type AgentDefinition } from "./types";

// One agent tile in the roster grid: identity + the at-a-glance authority signals
// (model tier, security veto, fail-open/closed posture, supervisor).
export function AgentCard({
  agent,
  selected,
  onSelect,
}: {
  agent: AgentDefinition;
  selected: boolean;
  onSelect: (agentId: string) => void;
}) {
  const { failure_policy: failure, authority } = agent;
  return (
    <button
      type="button"
      onClick={() => onSelect(agent.agent_id)}
      aria-pressed={selected}
      className={cn(
        "flex flex-col gap-2 rounded-lg border bg-card p-4 text-left transition-colors",
        "hover:border-ring/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
        selected && "border-primary ring-2 ring-primary/40",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium">{agent.display_name}</p>
          <p className="truncate font-mono text-xs text-muted-foreground">
            {agent.agent_id}
          </p>
        </div>
        <Badge variant={MODEL_TIER_VARIANT[agent.model_policy.tier]}>
          {agent.model_policy.tier}
        </Badge>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {authority.veto ? <Badge variant="destructive">veto</Badge> : null}
        {failure.mode === "fail_closed" ? (
          <Badge variant="outline" className="text-destructive">
            fail-closed
          </Badge>
        ) : null}
        {failure.mode === "fail_open" ? (
          <Badge variant="secondary">fail-open</Badge>
        ) : null}
        {failure.watchdog_restart ? (
          <Badge variant="outline">watchdog</Badge>
        ) : null}
      </div>

      <p className="text-xs text-muted-foreground">
        reports to{" "}
        <span className="font-mono text-foreground">{agent.supervisor}</span>
      </p>
    </button>
  );
}
