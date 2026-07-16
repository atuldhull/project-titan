"use client";

import { CheckCircle2, Circle, CircleDot } from "lucide-react";

import type { components } from "@titan/schemas";

import { Skeleton } from "@/components/ui/skeleton";
import { useMilestones } from "@/lib/projects/hooks";
import { cn } from "@/lib/utils";

type MilestoneOut = components["schemas"]["MilestoneOut"];

/**
 * MilestoneOut exposes title, status and due_at — that is the whole timeline.
 *
 * The status vocabulary is exactly `open | closed` (projects/service.py's
 * `MilestoneStatus` Literal; `open` is the column default and those two are the
 * only values any writer sets). Matching on done/complete/in_progress would
 * render every real milestone as "not started".
 */
export function milestoneIcon(status: string) {
  if (status === "closed") {
    return <CheckCircle2 aria-hidden className="size-4 text-success" />;
  }
  if (status === "open") {
    return <CircleDot aria-hidden className="size-4 text-primary" />;
  }
  return <Circle aria-hidden className="size-4 text-muted-foreground" />;
}

export function MilestoneTimeline({ projectId }: { projectId: string }) {
  const { data, isLoading, isError, refetch } = useMilestones(projectId);
  const milestones: MilestoneOut[] = data?.items ?? [];

  return (
    <section className="rounded-lg border bg-card">
      <header className="border-b px-4 py-2">
        <h3 className="text-sm font-semibold">Milestones</h3>
      </header>
      <div className="p-4">
        {isLoading ? (
          <ol className="space-y-3">
            {[0, 1, 2].map((i) => (
              <li key={i} className="flex items-center gap-3">
                <Skeleton className="size-4 rounded-full" />
                <Skeleton className="h-4 w-40" />
              </li>
            ))}
          </ol>
        ) : isError ? (
          <p className="text-sm text-destructive">
            Couldn’t load milestones.{" "}
            <button
              type="button"
              className="underline underline-offset-4"
              onClick={() => void refetch()}
            >
              Retry
            </button>
          </p>
        ) : milestones.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No milestones yet — they appear once planning produces a plan.
          </p>
        ) : (
          <ol className="relative space-y-4 border-l pl-5">
            {milestones.map((milestone) => (
              <li key={milestone.id} className="relative">
                <span className="absolute top-0.5 -left-[27px] bg-card">
                  {milestoneIcon(milestone.status)}
                </span>
                <p
                  className={cn(
                    "text-sm font-medium",
                    milestone.status === "closed" &&
                      "text-muted-foreground line-through",
                  )}
                >
                  {milestone.title}
                </p>
                <p className="text-xs text-muted-foreground capitalize">
                  {milestone.status}
                  {milestone.due_at
                    ? ` · due ${new Date(milestone.due_at).toLocaleDateString()}`
                    : ""}
                </p>
              </li>
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}
