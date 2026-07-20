"use client";

import Link from "next/link";

import type { components } from "@titan/schemas";

import { FOCUS_RING } from "@/components/ui/focus-ring";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { boardStats } from "@/lib/projects/derive";
import type { ProjectAgentCounts } from "@/lib/projects/derive";
import { cn } from "@/lib/utils";

type ProjectOut = components["schemas"]["ProjectOut"];
type Board = components["schemas"]["Board"];
type SprintOut = components["schemas"]["SprintOut"];

/**
 * An active-project card (§6.1): progress, sprint, agent count, CI status.
 *
 * Progress is derived from the Phase-41 board and agent counts from the agent
 * list — neither exists as a field on ProjectOut. CI has no backend source at
 * all, so it renders as §6.2's `CI –` (none/pending) rather than a fabricated
 * check.
 */
export function ProjectCard({
  project,
  board,
  boardLoading,
  sprint,
  agents,
}: {
  project: ProjectOut;
  board: Board | undefined;
  boardLoading?: boolean;
  sprint?: SprintOut;
  agents?: ProjectAgentCounts;
}) {
  const stats = boardStats(board);
  const agentCount = agents?.total ?? 0;

  return (
    <Link
      href={`/projects/${project.id}`}
      // TITAN-FRT-042: was `focus-visible:outline-none` with no replacement, so
      // the Overview's main navigation targets were invisible to a keyboard
      // user (`hover:border-ring` is pointer-only). FOCUS_RING is the shared §9
      // ring; the hover border stays as the separate pointer affordance.
      className={cn(
        "block rounded-lg border bg-card p-4 transition-colors hover:border-ring",
        FOCUS_RING,
      )}
    >
      <div className="flex items-baseline gap-2">
        <span className="truncate font-medium">{project.display_name}</span>
        <span className="ml-auto shrink-0 text-sm tabular-nums text-muted-foreground">
          {boardLoading ? (
            <Skeleton className="inline-block h-4 w-8 align-middle" />
          ) : stats.progressPct === null ? (
            "—"
          ) : (
            `${stats.progressPct}%`
          )}
        </span>
      </div>

      <Progress
        className="mt-2"
        value={stats.progressPct}
        aria-label={`${project.display_name} progress`}
      />

      <div className="mt-2 flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
        {/* Only claim a sprint when one was actually fetched. The Overview does
            not fan out /sprints per card, so it passes none — and rendering "no
            sprint" for an unfetched sprint would state something we don't know
            (the detail screen, which does fetch, shows the real one). */}
        {sprint ? (
          <>
            <span>sprint {sprint.number}</span>
            <span aria-hidden>·</span>
          </>
        ) : null}
        <span>
          {agentCount} {agentCount === 1 ? "agent" : "agents"}
        </span>
        <span aria-hidden>·</span>
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="cursor-help">CI –</span>
          </TooltipTrigger>
          <TooltipContent className="max-w-64">
            No CI signal is exposed by the backend yet, so this stays “–”
            (§6.2’s none/pending glyph) rather than showing a status we can’t
            verify.
          </TooltipContent>
        </Tooltip>
        {stats.blocked > 0 ? (
          <>
            <span aria-hidden>·</span>
            <span className="font-medium text-warning">
              {stats.blocked} blocked
            </span>
          </>
        ) : null}
      </div>
    </Link>
  );
}

export function ProjectCardSkeleton() {
  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="flex items-baseline gap-2">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="ml-auto h-4 w-8" />
      </div>
      <Skeleton className="mt-3 h-1.5 w-full" />
      <Skeleton className="mt-3 h-3 w-40" />
    </div>
  );
}
