"use client";

/**
 * One board column (Vol 4 §6.3).
 *
 * A column IS a workflow status, straight from `GET /api/v1/workflow` — the
 * spec's "configurable per project" columns do not exist, and neither does the
 * "Volume 6 workflow definition" it attributes them to (Volume 6 never mentions
 * one). The machine is global, so every project's board has the same columns.
 *
 * **No WIP-limit indicator**, for the same reason: nothing in Volume 6 or the
 * backend defines a WIP limit, per column or otherwise. There is no value to
 * read and nothing enforces one, so a "6/5" badge here would be a number this
 * component made up. The count is shown; the limit is not invented.
 */

import { useDroppable } from "@dnd-kit/core";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { statusLabel } from "@/lib/kanban/workflow";
import { cn } from "@/lib/utils";

export function BoardColumn({
  status,
  count,
  /** True while a drag is active AND this column would accept that card. */
  isDropTarget,
  isRejecting,
  stale,
  children,
}: {
  status: string;
  count: number;
  isDropTarget: boolean;
  isRejecting: boolean;
  /** This column's realtime stream is behind — its cards may be out of date. */
  stale: boolean;
  children: React.ReactNode;
}) {
  const { setNodeRef, isOver } = useDroppable({ id: status });

  return (
    <section
      ref={setNodeRef}
      aria-label={`${statusLabel(status)}, ${count} ${count === 1 ? "task" : "tasks"}`}
      data-testid={`column-${status}`}
      data-drop-target={isDropTarget ? "true" : undefined}
      className={cn(
        "flex w-64 shrink-0 flex-col rounded-lg border bg-muted/30",
        isOver && isDropTarget && "border-primary bg-accent",
        // A column that cannot take the dragged card is dimmed rather than
        // hidden: the board's shape must not change under a dragging hand.
        isRejecting && "opacity-50",
      )}
    >
      <header className="flex items-center gap-2 border-b px-2 py-1.5">
        <h3 className="text-xs font-semibold uppercase tracking-wide">
          {statusLabel(status)}
        </h3>
        <Badge variant="secondary" className="px-1 py-0 text-[10px]">
          {count}
        </Badge>
        {stale ? (
          <Badge
            variant="outline"
            className="ml-auto border-warning px-1 py-0 text-[10px] text-warning"
            title="This column's live updates were interrupted; it may be out of date."
          >
            Stale
          </Badge>
        ) : null}
      </header>
      <ul className="flex min-h-24 flex-1 flex-col gap-1.5 p-1.5">
        {children}
      </ul>
    </section>
  );
}

/** Empty: the board exists, planning has not produced tasks yet (§6.3). */
export function BoardEmpty() {
  return (
    <div className="rounded-lg border border-dashed p-8 text-center">
      <p className="text-sm font-medium">No tasks yet</p>
      <p className="mt-1 text-sm text-muted-foreground">
        The Project Manager Agent creates tasks after planning completes.
      </p>
    </div>
  );
}

/** Error: a board-level retry panel (§6.3). */
export function BoardError({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-dashed p-6 text-center"
    >
      <p className="text-sm text-destructive">Couldn’t load the board.</p>
      <Button variant="outline" size="sm" className="mt-2" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

/** Loading: 5 skeleton columns x 3 skeleton cards, exactly per §6.3. */
export function BoardSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading board" className="flex gap-3">
      {Array.from({ length: 5 }, (_, col) => (
        <div
          key={col}
          className="flex w-64 shrink-0 flex-col rounded-lg border"
        >
          <div className="border-b px-2 py-1.5">
            <Skeleton className="h-3 w-20" />
          </div>
          <div className="flex flex-col gap-1.5 p-1.5">
            {Array.from({ length: 3 }, (_, card) => (
              <Skeleton key={card} className="h-14 w-full" />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
