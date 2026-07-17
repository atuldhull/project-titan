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
import { initials } from "@/lib/agents/initials";
import { cn } from "@/lib/utils";
import type { components } from "@titan/schemas";

type AgentOut = components["schemas"]["AgentOut"];

// `initials` moved to lib/agents so the graph's nodes can share it: TITAN-FRT-049
// forbids components/graph importing from components/kanban.
export { initials };

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
