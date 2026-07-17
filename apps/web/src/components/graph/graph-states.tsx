"use client";

/**
 * The graph screens' empty/error states.
 *
 * Duplicated from the board's rather than shared: TITAN-FRT-049 forbids
 * `components/graph` importing `components/kanban`, and lifting a
 * board-specific empty message into `components/ui` would put a sentence about
 * the Project Manager Agent in a primitives directory. §6.4 explicitly says its
 * empty state is "same as board empty", so the duplication is the spec's, and it
 * is six lines.
 */

import { Button } from "@/components/ui/button";

/** §6.4: "empty = same as board empty". */
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

export function BoardError({ onRetry }: { onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-dashed p-6 text-center"
    >
      <p className="text-sm text-destructive">Couldn’t load the graph.</p>
      <Button variant="outline" size="sm" className="mt-2" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

/** §6.8's empty: the architecture is a pipeline OUTPUT, so say which stage. */
export function ArchitectureEmpty({ projectId }: { projectId: string }) {
  return (
    <div className="rounded-lg border border-dashed p-8 text-center">
      <p className="text-sm font-medium">Architecture not yet designed</p>
      <p className="mt-1 text-sm text-muted-foreground">
        It appears once the Architect Agent commits the design package at the
        architecture stage.
      </p>
      <Button variant="outline" size="sm" className="mt-3" asChild>
        <a href={`/projects/${projectId}?tab=overview`}>Project status</a>
      </Button>
    </div>
  );
}
