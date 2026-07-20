"use client";

/**
 * One log line in the virtualized list (§6.7).
 *
 * Kept dumb on purpose: every decision it could make is made in `lib/logs` and
 * passed in. jsdom renders zero rows for a virtualized list, so anything
 * decided in here is untestable by construction.
 */
import { memo } from "react";
import {
  AlertTriangle,
  Bug,
  ChevronRight,
  CircleAlert,
  Info,
  OctagonAlert,
} from "lucide-react";

import type { LogLine } from "@titan/api-client";

import { cn } from "@/lib/utils";
import { FOCUS_RING, TOUCH_TARGET_PSEUDO } from "@/components/ui/focus-ring";
import { formatClock } from "@/lib/logs/export";
import { highlightRanges, segment, type LogFilters } from "@/lib/logs/filters";

/**
 * Level styling. TITAN-FRT-044: never colour alone — every level pairs its
 * colour with a distinct icon SHAPE and keeps its text label, so the list is
 * readable in greyscale and to a colour-blind reader.
 */
const LEVEL_STYLE: Record<
  string,
  { icon: typeof Info; className: string; label: string }
> = {
  debug: { icon: Bug, className: "text-muted-foreground", label: "DEBUG" },
  info: {
    icon: Info,
    className: "text-primary",
    label: "INFO",
  },
  warning: {
    icon: AlertTriangle,
    className: "text-warning",
    label: "WARN",
  },
  error: { icon: CircleAlert, className: "text-destructive", label: "ERROR" },
  critical: {
    icon: OctagonAlert,
    className: "text-destructive font-bold",
    label: "CRIT",
  },
};

const UNKNOWN = {
  icon: Info,
  className: "text-muted-foreground",
  label: "—",
};

export function levelStyle(level: string | null | undefined) {
  // A stream with no `level` label is normal, not an error: postgres and redis
  // log plain text, so promtail's json stage extracts nothing to promote.
  return (level && LEVEL_STYLE[level]) || UNKNOWN;
}

export interface LogRowProps {
  readonly line: LogLine;
  readonly filters: LogFilters;
  readonly regex: RegExp | null;
  readonly expanded: boolean;
  readonly wrap: boolean;
  readonly onToggle: (id: string) => void;
  readonly onOpenContext: (line: LogLine) => void;
}

function LogRowImpl({
  line,
  filters,
  regex,
  expanded,
  wrap,
  onToggle,
  onOpenContext,
}: LogRowProps) {
  const style = levelStyle(line.level);
  const Icon = style.icon;
  const segments = segment(
    line.line,
    highlightRanges(line.line, filters, regex),
  );
  const hasFields = Object.keys(line.fields ?? {}).length > 0;

  return (
    <div className="group font-mono text-xs leading-5">
      <div className="flex items-start gap-2 px-2 py-0.5 hover:bg-muted/50">
        <button
          type="button"
          onClick={() => onToggle(line.id)}
          /* The 12px chevron IS the design — growing its painted box would
             wreck a dense log line, and this is the most-repeated control on
             the screen §8 names explicitly for the <768 band. So the box stays
             and an invisible 44px hit area is projected around it instead.
             FOCUS_RING also replaces the hand-rolled 1px ring with §9's
             2px/2px (TITAN-FRT-042). */
          className={cn(
            "mt-0.5 shrink-0 rounded text-muted-foreground hover:text-foreground",
            TOUCH_TARGET_PSEUDO,
            FOCUS_RING,
          )}
          aria-expanded={expanded}
          aria-label={expanded ? "Collapse line" : "Expand line"}
        >
          <ChevronRight
            className={cn(
              "h-3 w-3 transition-transform",
              expanded && "rotate-90",
            )}
          />
        </button>

        {/*
         * §8: "Logs keeps virtualized list with simplified filters" — and must
         * be legible at 375px. The old shape was one flex row of four fixed
         * `shrink-0` columns plus the message: timestamp (12 mono chars) + level
         * (w-16) + service (w-20) ate ~230px of a 343px content box, leaving the
         * message a truncated sliver — the only column anyone reads.
         *
         * So the metadata and the message stack under md and sit side-by-side at
         * md+. `flex-col md:flex-row` rather than wrapping the outer row: the
         * chevron and the context button must stay on the FIRST line at every
         * width, and `flex-wrap` cannot promise that.
         *
         * Height: this makes a narrow row ~2 lines instead of 1. The virtualizer
         * does not care — `logs-screen` passes `ref={virtualizer.measureElement}`
         * on every item, so heights are measured, not assumed, and mixed heights
         * are already load-bearing there (a GapMarker is several lines tall and
         * has always ridden the same path). `ROW_HEIGHT` remains the pre-measure
         * estimate only; see its comment.
         */}
        <div className="flex min-w-0 flex-1 flex-col gap-x-2 md:flex-row md:items-start">
          <div className="flex shrink-0 items-center gap-2">
            <time
              className="shrink-0 text-muted-foreground tabular-nums"
              dateTime={line.ts_ns}
            >
              {formatClock(line.ts_ns)}
            </time>

            <span
              className={cn(
                "flex w-16 shrink-0 items-center gap-1",
                style.className,
              )}
            >
              <Icon className="h-3 w-3 shrink-0" aria-hidden />
              {style.label}
            </span>

            <span
              className="w-20 shrink-0 truncate text-muted-foreground"
              title={line.labels?.service}
            >
              {line.labels?.service ?? "—"}
            </span>
          </div>

          <span
            className={cn(
              "min-w-0 md:flex-1",
              wrap ? "whitespace-pre-wrap break-all" : "truncate",
            )}
          >
            {segments.map((seg, i) =>
              seg.match ? (
                <mark
                  key={i}
                  className="rounded-sm bg-amber-300/60 text-inherit dark:bg-amber-500/40"
                >
                  {seg.text}
                </mark>
              ) : (
                // The event text is the readable summary; the raw line is what
                // is shown, because that is what greps and what Loki holds.
                <span key={i}>{seg.text}</span>
              ),
            )}
          </span>
        </div>

        {(line.level === "error" || line.level === "critical") && (
          <button
            type="button"
            onClick={() => onOpenContext(line)}
            // Opacity-only reveal would make this unreachable by keyboard, so
            // focus-within brings it back and it stays in the tab order.
            //
            // `opacity-0` also puts it out of reach on touch, where there is no
            // hover at all — so under md it is simply always visible. It is one
            // glyph; the density it costs is worth an action that would
            // otherwise be unreachable on the band §8 cares most about.
            className={cn(
              "shrink-0 rounded px-1 text-muted-foreground transition-opacity hover:text-foreground focus:opacity-100 md:opacity-0 md:group-hover:opacity-100",
              // 20x20 painted; the hit area is projected out to 44px rather
              // than inflating a glyph in a dense mono row (§8).
              TOUCH_TARGET_PSEUDO,
              FOCUS_RING,
            )}
            title="Show surrounding lines"
          >
            ⧉
          </button>
        )}
      </div>

      {expanded && (
        <dl className="ml-8 mb-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 border-l pl-3 text-[11px] text-muted-foreground">
          {/* Vol 9 §6.3 describes an `attrs` object; structlog has no such
              thing — these are the caller's kwargs, which the backend
              reconstructs by subtracting its own pipeline's keys. */}
          <Field label="module" value={line.module} />
          <Field label="request_id" value={line.request_id} />
          <Field label="trace_id" value={line.trace_id} />
          <Field label="project_id" value={line.project_id} />
          <Field label="task_id" value={line.task_id} />
          <Field label="agent_id" value={line.agent_id} />
          {hasFields &&
            Object.entries(line.fields ?? {}).map(([key, value]) => (
              <Field
                key={key}
                label={key}
                value={
                  typeof value === "string" ? value : JSON.stringify(value)
                }
              />
            ))}
        </dl>
      )}
    </div>
  );
}

function Field({
  label,
  value,
}: {
  label: string;
  value: string | null | undefined;
}) {
  if (!value) return null;
  return (
    <>
      <dt className="text-muted-foreground/70">{label}</dt>
      <dd className="break-all text-foreground/80">{value}</dd>
    </>
  );
}

/**
 * Memoised: a tail append re-renders the list, and without this every visible
 * row would re-run its highlight scan even though only the new rows changed.
 */
export const LogRow = memo(LogRowImpl);
