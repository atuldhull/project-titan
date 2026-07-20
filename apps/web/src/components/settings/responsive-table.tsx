"use client";

import { Fragment, type ReactNode } from "react";

import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

/**
 * One row model, two layouts: a real `<table>` at ≥768 and a card list below
 * (Vol 4 §8, "tables → card lists").
 *
 * **Why a component rather than two blocks of JSX per screen.** Every Settings
 * table here has between three and six columns, and the failure mode of writing
 * the narrow layout by hand is not that it looks wrong — it is that someone adds
 * a seventh column to the table six months later and the card silently keeps
 * showing six. Columns are declared ONCE; both renderers consume the same array,
 * so a card cannot fall behind a table.
 *
 * **Why the cards are a `<dl>` and not a stripped-down table.** TITAN-FRT-043
 * wants "tables as tables", and this deliberately stops being one below 768 —
 * a table whose header row has scrolled out of sight conveys nothing, which is
 * the actual experience of `whitespace-nowrap` columns (`ui/table.tsx:76,89`) in
 * a 343px port. A definition list keeps the thing that mattered: every value is
 * still explicitly bound to its column name, for a screen reader as much as for
 * an eye. `components/ui/table` is another agent's file, so the swap happens
 * here at the call sites rather than inside the primitive.
 */
export interface ResponsiveColumn<T> {
  /** Stable identity; the React key in both layouts. */
  readonly key: string;
  /** Column heading, and the `<dt>` label on a card. */
  readonly header: ReactNode;
  readonly cell: (row: T) => ReactNode;
  /**
   * Right/centre alignment for the TABLE only. Cards are always a left-aligned
   * label/value pair — a centred value under a left-aligned label reads as a
   * layout bug rather than as a numeric column.
   */
  readonly align?: "right" | "center";
  /**
   * Renders as the card's title instead of a labelled row (and is unchanged in
   * the table). At most one column should set it; if none does, the card is a
   * plain list of labelled values.
   */
  readonly primary?: boolean;
}

export function ResponsiveTable<T>({
  rows,
  rowKey,
  columns,
  caption,
  rowClassName,
}: {
  readonly rows: readonly T[];
  readonly rowKey: (row: T) => string;
  readonly columns: readonly ResponsiveColumn<T>[];
  /**
   * Names the table for assistive tech. Rendered `sr-only` — every call site
   * already has a visible CardTitle above it, and a second visible heading
   * would be noise.
   */
  readonly caption: string;
  /** Per-row styling that applies to both layouts (e.g. dimming a revoked key). */
  readonly rowClassName?: (row: T) => string | undefined;
}) {
  const primary = columns.find((column) => column.primary);
  const secondary = columns.filter((column) => !column.primary);

  return (
    <>
      {/*
       * `hidden md:table` sits on the <table> itself: `ui/table` wraps it in its
       * own `overflow-x-auto` div, and that wrapper is the scroll port we still
       * want at md+. Hiding the table collapses the wrapper to nothing, so the
       * two layouts never both occupy space.
       */}
      <Table className="hidden md:table">
        <TableCaption className="sr-only">{caption}</TableCaption>
        <TableHeader>
          <TableRow>
            {columns.map((column) => (
              <TableHead key={column.key} className={alignClass(column.align)}>
                {column.header}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={rowKey(row)} className={rowClassName?.(row)}>
              {columns.map((column) => (
                <TableCell
                  key={column.key}
                  className={alignClass(column.align)}
                >
                  {column.cell(row)}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>

      <ul aria-label={caption} className="space-y-2 md:hidden">
        {rows.map((row) => (
          <li
            key={rowKey(row)}
            className={cn(
              "rounded-lg border bg-card p-3 text-sm",
              rowClassName?.(row),
            )}
          >
            {primary ? (
              <div className="mb-2 font-medium break-words">
                {primary.cell(row)}
              </div>
            ) : null}
            <dl className="grid grid-cols-[minmax(5rem,auto)_1fr] gap-x-3 gap-y-1">
              {secondary.map((column) => (
                <Fragment key={column.key}>
                  <dt className="text-xs text-muted-foreground">
                    {column.header}
                  </dt>
                  {/* `min-w-0` so a long unbreakable value (an endpoint URL, a
                      model id) shrinks its own grid track instead of widening
                      the card past the viewport — the exact way a "responsive"
                      card list still ends up scrolling the body sideways. */}
                  <dd className="min-w-0 break-words">{column.cell(row)}</dd>
                </Fragment>
              ))}
            </dl>
          </li>
        ))}
      </ul>
    </>
  );
}

function alignClass(
  align: ResponsiveColumn<never>["align"],
): string | undefined {
  if (align === "right") return "text-right";
  if (align === "center") return "text-center";
  return undefined;
}
