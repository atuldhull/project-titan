"use client";

/**
 * One task card (Vol 4 §6.3).
 *
 * §6.3's card is "ID, title, agent avatar, points, labels, blocked badge". Three
 * of those are real, and the deviations are deliberate:
 *
 * - **ID** — there is no "T-231". Tasks are keyed by UUIDv7 and the table has no
 *   number/key/slug column (sprints have one; tasks never got it). A short prefix
 *   is shown: v7 ids are time-ordered, so it sorts sensibly, but it is not a
 *   human-memorable key and is not claimed to be one.
 * - **Labels** — do not exist. No column, no table, no endpoint, in any
 *   migration. Nothing is rendered rather than inventing a taxonomy.
 * - **Blocked badge** — real, and TWO distinct things that §6.3 conflates. A card
 *   can be in the `blocked` STATUS (its own column, entered via the `block`
 *   action) and/or have unmet DEPENDENCIES (derived from the graph). The badge
 *   here is the dependency one, because the status is already the column.
 */

import { useDraggable } from "@dnd-kit/core";
import { CircleAlert, GripVertical } from "lucide-react";

import type { components } from "@titan/schemas";

import { AgentAvatar } from "@/components/kanban/agent-avatar";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type { Card } from "@/lib/kanban/board-store";
import { isNavigationKey } from "@/lib/kanban/navigation";
import { cn } from "@/lib/utils";

type AgentOut = components["schemas"]["AgentOut"];

/** The rendered id: a UUIDv7 prefix, not the "T-231" §6.3 imagines. */
export function shortTaskId(id: string): string {
  return id.slice(0, 8);
}

export function TaskCard({
  card,
  agent,
  unmetDeps,
  draggable,
  pulsing,
  animating,
  onOpen,
  onNavigate,
  registerRef,
}: {
  card: Card;
  agent: AgentOut | undefined;
  unmetDeps: number;
  draggable: boolean;
  /** The assigned agent just moved this card — pulse its avatar. */
  pulsing: boolean;
  /** Just moved by an agent — plays the §6.3 300 ms settle. */
  animating: boolean;
  onOpen: (taskId: string) => void;
  /** An arrow key was pressed while this card had focus (§10). */
  onNavigate?: (taskId: string, key: string) => void;
  /** Lets the board move focus here when navigation lands on this card. */
  registerRef?: (taskId: string, node: HTMLElement | null) => void;
}) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: card.id,
    disabled: !draggable,
    data: { status: card.status },
  });

  return (
    <li
      data-testid={`task-card-${card.id}`}
      data-animating={animating ? "true" : undefined}
      className={cn(
        "rounded-md border bg-card p-2 text-left shadow-xs",
        isDragging && "opacity-50",
        animating &&
          "motion-safe:animate-in motion-safe:fade-in motion-safe:duration-300",
      )}
    >
      {/* ONE control per card, deliberately. It is the tab stop, the drag
          handle and the open affordance at once: a separate grip button would
          announce every card twice to a screen reader and double the tab
          distance across a long column. §10's keys divide cleanly — Enter
          opens, Space lifts (the board narrows dnd-kit's start key to Space so
          Enter is free), arrows navigate while nothing is lifted. A pointer
          click still opens, because the drag sensor needs 4px of movement
          before it claims the gesture. */}
      <button
        type="button"
        ref={(node) => {
          setNodeRef(node);
          registerRef?.(card.id, node);
        }}
        {...(draggable ? listeners : {})}
        {...(draggable ? attributes : {})}
        onClick={() => onOpen(card.id)}
        onKeyDown={(event) => {
          // MUST come last and MUST delegate. dnd-kit's KeyboardSensor activator
          // IS an `onKeyDown` inside `listeners`, so this prop overwrites it —
          // an `onKeyDown` that returns without calling through silently makes
          // every card unliftable, with no type error and no failing render.
          if (isNavigationKey(event.key) && !isDragging) {
            // Not lifted: arrows move FOCUS between cards. Once lifted, dnd-kit
            // owns the arrows and this must keep its hands off.
            event.preventDefault();
            onNavigate?.(card.id, event.key);
            return;
          }
          if (event.key === "Enter" && !isDragging) {
            // Mid-drag, Enter is not ours either: opening a Sheet over a lifted
            // card strands the drag behind a modal.
            event.preventDefault();
            onOpen(card.id);
            return;
          }
          // Space lifts, Space drops, Escape cancels — all dnd-kit's.
          if (draggable) {
            listeners?.onKeyDown?.(event);
          }
        }}
        aria-label={
          draggable
            ? `${card.title}. Press enter to open, space to move.`
            : `${card.title}. Press enter to open.`
        }
        className={cn(
          "flex w-full items-start gap-1 text-left",
          draggable && "cursor-grab",
        )}
      >
        {draggable ? (
          <GripVertical
            aria-hidden
            className="mt-0.5 size-3.5 shrink-0 text-muted-foreground"
          />
        ) : null}
        <span className="line-clamp-2 text-sm font-medium">{card.title}</span>
      </button>

      <div className="mt-1.5 flex flex-wrap items-center gap-1.5 pl-1">
        <span className="font-mono text-[10px] text-muted-foreground">
          {shortTaskId(card.id)}
        </span>
        {card.estimate_points !== null ? (
          <Badge variant="secondary" className="px-1 py-0 text-[10px]">
            {card.estimate_points} pts
          </Badge>
        ) : null}
        {unmetDeps > 0 ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <Badge
                variant="outline"
                className="gap-0.5 border-warning px-1 py-0 text-[10px] text-warning"
              >
                <CircleAlert aria-hidden className="size-2.5" />
                {unmetDeps}
              </Badge>
            </TooltipTrigger>
            <TooltipContent>
              Blocked: {unmetDeps}{" "}
              {unmetDeps === 1 ? "dependency" : "dependencies"} not done
            </TooltipContent>
          </Tooltip>
        ) : null}
        <span className="ml-auto">
          <AgentAvatar agent={agent} pulsing={pulsing} />
        </span>
      </div>
    </li>
  );
}
