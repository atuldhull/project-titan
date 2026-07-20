"use client";

/**
 * A sparkline and a stacked time-series, over the pure maths in `lib/monitoring/chart`.
 *
 * These hold no judgement: every decision that could be wrong (the scales, the
 * empty case, the stacking) is in the pure module, where jsdom's lack of layout
 * does not stop it being tested.
 */
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  alignSeries,
  areaPath,
  colorFor,
  dashFor,
  extentOf,
  linePath,
  stack,
  type Series,
} from "@/lib/monitoring/chart";
import { cn } from "@/lib/utils";

const SPARK = { width: 88, height: 24, padding: 2 } as const;

export function Sparkline({
  points,
  className,
}: {
  readonly points: readonly { x: number; y: number }[];
  readonly className?: string;
}) {
  const series: Series[] = [{ key: "s", points }];
  const extent = extentOf(series);
  if (!extent) {
    return <span className="text-[10px] text-muted-foreground">no data</span>;
  }
  return (
    <svg
      width={SPARK.width}
      height={SPARK.height}
      viewBox={`0 0 ${SPARK.width} ${SPARK.height}`}
      className={cn("overflow-visible", className)}
      // Decorative: the number beside it is the accessible value.
      aria-hidden
      focusable="false"
    >
      <path
        d={areaPath(points, extent, SPARK)}
        fill="currentColor"
        opacity={0.15}
      />
      <path
        d={linePath(points, extent, SPARK)}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Legend/table share it, so a caller cannot format one and forget the other. */
type Format = (value: number) => string;

const DEFAULT_FORMAT: Format = (value) => value.toLocaleString();

/**
 * §6.10's stacked time series, plus its accessible alternative.
 *
 * **The prop is the RAW per-group series, not the stacked one.** It used to be
 * stacked by the caller, which was a live drift hazard the moment the table
 * below existed: a table built from cumulative data reports "openai cost $0.31"
 * when openai actually cost $0.11 and merely sits on anthropic's $0.20. Stacking
 * HERE means the drawn line and the tabulated number provably come from one
 * array, and the caller cannot get the order wrong.
 */
export function StackedSeries({
  series,
  height = 200,
  label,
  format = DEFAULT_FORMAT,
}: {
  readonly series: readonly Series[];
  readonly height?: number;
  readonly label: string;
  /** Renders a y value for the legend and the table (`formatUsd`, `formatTokens`). */
  readonly format?: Format;
}) {
  const box = { width: 720, height, padding: 8 };
  // `alignSeries` is idempotent and `stack` runs it internally, so these two
  // agree by construction: same x-axis, same order, same values before the
  // running total is applied.
  const aligned = useMemo(() => alignSeries(series), [series]);
  const stacked = useMemo(() => stack(aligned), [aligned]);
  const extent = extentOf(stacked);

  // TITAN-FRT-047's toggle. Under `md` the choice is not offered and the table
  // always wins — see the render below — so this only ever decides the ≥768
  // case, where the chart is legible and is therefore the default.
  const [tabular, setTabular] = useState(false);

  if (!extent) {
    return (
      <div
        className="flex items-center justify-center rounded-md border border-dashed text-xs text-muted-foreground"
        style={{ height }}
      >
        No data in range
      </div>
    );
  }

  return (
    <figure className="space-y-2">
      <div className="flex items-center justify-end">
        {/*
         * §9's TITAN-FRT-047 ("canvases provide an accessible alternative: a
         * structured list view … toggleable via 'List view'"), applied to the
         * chart for the same reason it applies to the graph canvas: an <svg>
         * with one aria-label is a single opaque string where the data is a
         * grid of numbers. `components/graph/list-view.tsx` is the house
         * pattern this mirrors.
         *
         * Hidden under `md` because below 768 the toggle would be a no-op: the
         * chart is `preserveAspectRatio="none"` into ~343px and is not readable
         * at any setting, so the table is unconditional there (§8's
         * "tables → card lists" cousin — the narrow layout keeps the DATA and
         * drops the picture, never the reverse).
         */}
        <div className="hidden items-center gap-1 md:flex">
          <Button
            size="sm"
            variant={tabular ? "ghost" : "secondary"}
            aria-pressed={!tabular}
            className="h-6 px-2 text-[11px]"
            onClick={() => setTabular(false)}
          >
            Chart
          </Button>
          <Button
            size="sm"
            variant={tabular ? "secondary" : "ghost"}
            aria-pressed={tabular}
            className="h-6 px-2 text-[11px]"
            onClick={() => setTabular(true)}
          >
            List view
          </Button>
        </div>
      </div>

      {tabular ? null : (
        <svg
          viewBox={`0 0 ${box.width} ${box.height}`}
          // `hidden md:block`, not a JS breakpoint: there is no matchMedia in
          // this app and no jsdom mock for it, so a `useMediaQuery` here would
          // break the vitest suite to say something CSS already knows.
          className="hidden w-full md:block"
          role="img"
          aria-label={label}
          preserveAspectRatio="none"
        >
          {stacked.map((s, i) => (
            <path
              key={s.key}
              d={linePath(s.points, extent, box)}
              fill="none"
              stroke={colorFor(s.key, i)}
              // The non-colour channel (TITAN-FRT-044) and the thing that keeps
              // series 7 distinct from series 1 once the 6-colour palette wraps.
              strokeDasharray={dashFor(i)}
              strokeWidth={2}
              strokeLinejoin="round"
              // Keeps both the width AND the dash rhythm in screen space, so
              // `preserveAspectRatio="none"` cannot stretch a dotted line into
              // something that reads as dashed.
              vectorEffect="non-scaling-stroke"
            />
          ))}
        </svg>
      )}

      {/*
       * ALWAYS rendered, with only its visibility varying: `md:hidden` when the
       * chart is showing (so narrow viewports get the table and wide ones get
       * the picture), unconditional once the toggle picks it. Mounting it either
       * way is what makes the two views incapable of disagreeing.
       */}
      <SeriesTable
        series={aligned}
        label={label}
        format={format}
        className={tabular ? undefined : "md:hidden"}
      />

      {tabular ? null : (
        <figcaption className="hidden flex-wrap gap-3 md:flex">
          {stacked.map((s, i) => (
            <span key={s.key} className="flex items-center gap-1.5 text-[11px]">
              {/* A 24×8 line segment, not the old 8×8 square: a swatch has to
                  be long enough to show a dash rhythm, or the second encoding
                  channel exists in the chart and not in the key that explains
                  it. Still paired with the key's text, so the legend survives
                  greyscale on its own. */}
              <svg
                width={24}
                height={8}
                viewBox="0 0 24 8"
                className="shrink-0"
                aria-hidden
                focusable="false"
              >
                <line
                  x1={0}
                  y1={4}
                  x2={24}
                  y2={4}
                  stroke={colorFor(s.key, i)}
                  strokeDasharray={dashFor(i)}
                  strokeWidth={2}
                />
              </svg>
              <span className="font-mono text-muted-foreground">{s.key}</span>
            </span>
          ))}
        </figcaption>
      )}
    </figure>
  );
}

/**
 * The series as a real table — TITAN-FRT-047's "same data", and the only
 * readable form of this chart below 768.
 *
 * One row per bucket, one column per group, because that is the shape a reader
 * asks questions of ("what did anthropic cost at 14:00"). The values are the
 * ALIGNED raw series, so a bucket a group is missing from reads 0 rather than
 * inheriting the group below it.
 */
function SeriesTable({
  series,
  label,
  format,
  className,
}: {
  readonly series: readonly Series[];
  readonly label: string;
  readonly format: Format;
  readonly className?: string;
}) {
  // Every series carries the same x-axis after `alignSeries`, so the first one
  // defines the rows; `?? []` covers the degenerate no-series case, which the
  // extent check upstream already rejects but which must not throw here.
  const buckets = series[0]?.points.map((p) => p.x) ?? [];

  return (
    <div
      className={cn(
        // Its own scroll port: one column per group and the group count is
        // server-driven, so this table CAN be wider than the viewport — and §8
        // forbids the body ever scrolling sideways for it.
        "max-h-80 overflow-auto rounded-md border",
        className,
      )}
    >
      <table className="w-full text-left text-xs">
        <caption className="sr-only">
          {label} — {buckets.length} time buckets across {series.length} series.
        </caption>
        <thead className="sticky top-0 bg-card text-muted-foreground">
          <tr>
            <th scope="col" className="px-2 py-1 font-medium">
              Time
            </th>
            {series.map((s) => (
              <th
                key={s.key}
                scope="col"
                className="px-2 py-1 text-right font-medium whitespace-nowrap"
              >
                {s.key}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="font-mono">
          {buckets.map((x, row) => (
            <tr key={x} className="border-t">
              <th
                scope="row"
                className="px-2 py-1 text-left font-normal whitespace-nowrap text-muted-foreground"
              >
                {new Date(x).toLocaleString()}
              </th>
              {series.map((s) => (
                <td
                  key={s.key}
                  className="px-2 py-1 text-right tabular-nums whitespace-nowrap"
                >
                  {format(s.points[row]?.y ?? 0)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
