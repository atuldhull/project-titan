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
import { CircleSlash } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { statusLabel } from "@/lib/kanban/workflow";
import { cn } from "@/lib/utils";

/**
 * The horizontal scroller the columns live in (§8: "Kanban horizontal-scrolls
 * with snap columns" at 768–1023).
 *
 * Shared by the live board and the skeleton on purpose — see `BoardSkeleton`.
 *
 * **Snap is `max-lg` only, and that is a decision rather than an oversight.**
 * §8 asks for snapping in the 768–1023 band and below, where a 14-column board
 * is several screens wide and a swipe that lands between two columns leaves you
 * reading two half-columns. At 1024+ the input is a trackpad or a wheel, where
 * `snap-mandatory` fights the user: every scroll gesture is re-aligned on
 * release, so you cannot park the viewport to compare two columns' edges. It
 * also fights dnd-kit's auto-scroll, which drives this container by setting
 * `scrollLeft` during a drag — mandatory snap re-aligns when that stops and
 * shifts the board under a held card. Under `lg` that trade is worth it because
 * the alternative (a half-column) is worse; above it, it is not.
 */
export const BOARD_SCROLLER_CLASS =
  "flex gap-3 overflow-x-auto pb-2 max-lg:snap-x max-lg:snap-mandatory";

/** One column's own box, shared with the skeleton so the two cannot drift. */
const COLUMN_CLASS = "flex w-64 shrink-0 flex-col rounded-lg max-lg:snap-start";

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
        COLUMN_CLASS,
        "border bg-muted/30",
        isOver && isDropTarget && "border-primary bg-accent",
        // A column that cannot take the dragged card is dimmed rather than
        // hidden: the board's shape must not change under a dragging hand.
        isRejecting && "opacity-50",
      )}
    >
      <header className="relative flex items-center gap-2 border-b px-2 py-1.5">
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
        {/* TITAN-FRT-044: "status is never colour-only". `opacity-50` above is a
            colour/alpha change and nothing else — in a grayscale audit, and to a
            screen reader, a refusing column is indistinguishable from an
            accepting one. This says it in a glyph AND in words, and it sits
            inside the section that already carries the column's accessible name
            so it is read with it.

            **Out of flow, and that is not cosmetic.** It renders exactly when a
            drag starts, on every column that is not a target — i.e. almost all
            of them. As an in-flow block it added a ~22 px row to each of those
            columns in the same commit that set `dragging`, which (a) shifts
            every card in them downward under a hand that is already holding
            one, contradicting the invariant stated on `isRejecting` ten lines
            above ("the board's shape must not change under a dragging hand"),
            and (b) moves the cards after dnd-kit has taken its drag-start
            measurements of these containers. Absolutely positioned against the
            header it overlays the top of the card list instead, so the notice
            appears and the geometry does not move at all. */}
        {isRejecting ? (
          <p className="absolute inset-x-2 top-full z-10 mt-1 flex items-center gap-1 rounded-sm border bg-card px-1.5 py-0.5 text-[10px] text-muted-foreground shadow-xs">
            <CircleSlash aria-hidden className="size-3 shrink-0" />
            Won’t accept this card
          </p>
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
      {/* §8: ≥44 px touch targets under `md`. `size="sm"` is 32 px tall, and
          this is the only control on a screen that has otherwise failed — the
          one button on the page is the last one that should be hard to hit.
          The height is added here rather than in `ui/button.tsx`, which another
          agent owns and is giving its own touch step. */}
      <Button
        variant="outline"
        size="sm"
        className="mt-2 max-md:min-h-11 max-md:px-4"
        onClick={onRetry}
      >
        Retry
      </Button>
    </div>
  );
}

/**
 * Loading: 5 skeleton columns x 3 skeleton cards, exactly per §6.3.
 *
 * It uses the real board's scroller, which it did not before, and that was a
 * §8 violation with a visible symptom: five 256 px columns plus four 12 px gaps
 * is 1328 px of content in a plain `flex` row, so during EVERY board load the
 * content region — and at narrow widths the body with it — scrolled sideways,
 * then stopped when the data arrived and the real (scrolling) board replaced it.
 * "No horizontal body scroll ever" is not "except while loading".
 */
export function BoardSkeleton() {
  return (
    <div
      aria-busy="true"
      aria-label="Loading board"
      className={BOARD_SCROLLER_CLASS}
    >
      {Array.from({ length: 5 }, (_, col) => (
        <div key={col} className={cn(COLUMN_CLASS, "border")}>
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
