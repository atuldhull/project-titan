"use client";

/**
 * The canvas's dynamic-import boundary (TITAN-FRT-005).
 *
 * "Heavy visualizations (graphs, charts, terminals) SHALL be dynamically
 * imported." React Flow plus dagre plus the stylesheet is the heaviest thing in
 * this app; `next/dynamic` with `ssr: false` keeps all of it out of every other
 * route's shared chunk and out of the server bundle.
 *
 * `ssr: false` is required, not preference: React Flow measures the DOM with
 * ResizeObserver on mount, which does not exist on the server.
 *
 * Worth being precise about what this does and does not protect. The bundle gate
 * (`scripts/check-bundle-budget.mjs`) measures the `/org` route ONLY, so it would
 * not have caught a static import here — nothing on /org imports the graph.
 * Splitting is still right (it keeps React Flow out of the shared chunk that
 * every route pays for), but the gate is not what proves it; the build's
 * per-route First Load JS is.
 */

import dynamic from "next/dynamic";

import { Skeleton } from "@/components/ui/skeleton";
import { CANVAS_FRAME_CLASS } from "@/lib/graph/canvas-frame";
import { cn } from "@/lib/utils";

/**
 * §6.4/§6.8's loading state: a skeleton canvas that says what it is doing.
 *
 * Its frame comes from the same constant the real canvas uses. These two swap
 * places the instant the dynamic import resolves, so any disagreement about the
 * box is a visible page jump at exactly that moment (§8: no layout that shifts
 * under the reader) — the two `h-[70vh]` literals this replaces stayed equal
 * only by nobody having touched one of them.
 */
export function CanvasSkeleton() {
  return (
    <div
      aria-busy="true"
      aria-label="Computing layout"
      className={cn(CANVAS_FRAME_CLASS, "flex items-center justify-center")}
    >
      <div className="w-full max-w-md space-y-3 p-6 text-center">
        <Skeleton className="mx-auto h-3 w-32" />
        <div className="flex justify-center gap-3">
          <Skeleton className="h-12 w-32" />
          <Skeleton className="h-12 w-32" />
          <Skeleton className="h-12 w-32" />
        </div>
        <p className="text-xs text-muted-foreground">Computing layout…</p>
      </div>
    </div>
  );
}

export const GraphCanvas = dynamic(
  () => import("@/components/graph/graph-canvas").then((m) => m.GraphCanvas),
  { ssr: false, loading: () => <CanvasSkeleton /> },
);
