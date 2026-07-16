"use client";

/**
 * An agent's avatar on a task card (Vol 4 §6.3).
 *
 * There is no avatar to render. `AgentOut` is
 * `{id, project_id, role, name, status, model_binding, config, current_task_id,
 * …}` — no image, no icon, no colour, and the roster's `AgentDefinition` adds a
 * `display_name` but no art either. So this draws initials from the agent's name,
 * tinted by nothing, with the real name and role in a tooltip. It is an
 * identicon, not a photo, and pretending otherwise would mean inventing art for
 * a record that has none.
 */

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { components } from "@titan/schemas";

type AgentOut = components["schemas"]["AgentOut"];

/** Initials from an agent name: "backend-bot" -> "BB", "qa" -> "QA". */
export function initials(name: string): string {
  const parts = name.split(/[\s\-_.]+/u).filter(Boolean);
  if (parts.length === 0) {
    return "??";
  }
  if (parts.length === 1) {
    return parts[0].slice(0, 2).toUpperCase();
  }
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

export function AgentAvatar({
  agent,
  pulsing = false,
  className,
}: {
  agent: AgentOut | undefined;
  /** The agent just moved this card — pulse it for the duration (§6.3). */
  pulsing?: boolean;
  className?: string;
}) {
  if (!agent) {
    // Unassigned is a real, common state — say so rather than drawing a
    // placeholder head that implies someone is on it.
    return (
      <span
        aria-label="Unassigned"
        className={cn(
          "inline-flex size-5 items-center justify-center rounded-full border border-dashed border-muted-foreground/40 text-[9px] text-muted-foreground",
          className,
        )}
      >
        —
      </span>
    );
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid={`agent-avatar-${agent.id}`}
          data-pulsing={pulsing ? "true" : undefined}
          className={cn(
            "inline-flex size-5 items-center justify-center rounded-full bg-secondary text-[9px] font-semibold text-secondary-foreground",
            pulsing && "animate-pulse ring-2 ring-primary",
            className,
          )}
        >
          {initials(agent.name)}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        {agent.name} · {agent.role}
      </TooltipContent>
    </Tooltip>
  );
}
