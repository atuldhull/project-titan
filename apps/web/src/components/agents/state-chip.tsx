"use client";

import { Badge } from "@/components/ui/badge";
import type { AgentState } from "@/lib/agents/derive";

/**
 * The §6.5 state chip.
 *
 * Four states, not five: `blocked` is missing because it has no agent-level
 * meaning here (a task can be blocked; an agent cannot), and `working` is what
 * §6.5 calls the `busy`/running condition.
 *
 * The glyph carries the same information as the colour — TITAN-FRT-011: colour
 * is never the only channel.
 */
const CHIP: Record<
  AgentState,
  {
    label: string;
    glyph: string;
    variant: "default" | "secondary" | "outline" | "destructive";
  }
> = {
  working: { label: "working", glyph: "●", variant: "default" },
  idle: { label: "idle", glyph: "○", variant: "outline" },
  paused: { label: "paused", glyph: "❚❚", variant: "secondary" },
  error: { label: "error", glyph: "✖", variant: "destructive" },
};

export function StateChip({ state }: { state: AgentState }) {
  const chip = CHIP[state];
  return (
    <Badge variant={chip.variant} className="gap-1">
      <span aria-hidden>{chip.glyph}</span>
      {chip.label}
    </Badge>
  );
}

export const STATE_LABEL: Record<AgentState, string> = {
  working: CHIP.working.label,
  idle: CHIP.idle.label,
  paused: CHIP.paused.label,
  error: CHIP.error.label,
};
