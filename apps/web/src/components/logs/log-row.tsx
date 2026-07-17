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
    className: "text-sky-600 dark:text-sky-400",
    label: "INFO",
  },
  warning: {
    icon: AlertTriangle,
    className: "text-amber-600 dark:text-amber-400",
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
          className="mt-0.5 shrink-0 rounded text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
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

        <time
          className="shrink-0 text-muted-foreground tabular-nums"
          dateTime={line.ts_ns}
        >
          {formatClock(line.ts_ns)}
        </time>

        <span
          className={cn(
            "flex shrink-0 items-center gap-1 w-16",
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

        <span
          className={cn(
            "min-w-0 flex-1",
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
              // The event text is the readable summary; the raw line is what is
              // shown, because that is what greps and what Loki holds.
              <span key={i}>{seg.text}</span>
            ),
          )}
        </span>

        {(line.level === "error" || line.level === "critical") && (
          <button
            type="button"
            onClick={() => onOpenContext(line)}
            // Opacity-only reveal would make this unreachable by keyboard, so
            // focus-within brings it back and it stays in the tab order.
            className="shrink-0 rounded px-1 text-muted-foreground opacity-0 transition-opacity hover:text-foreground focus:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring group-hover:opacity-100"
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
