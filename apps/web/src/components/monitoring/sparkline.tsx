"use client";

/**
 * A sparkline and a stacked time-series, over the pure maths in `lib/monitoring/chart`.
 *
 * These hold no judgement: every decision that could be wrong (the scales, the
 * empty case, the stacking) is in the pure module, where jsdom's lack of layout
 * does not stop it being tested.
 */
import {
  areaPath,
  colorFor,
  extentOf,
  linePath,
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

export function StackedSeries({
  series,
  height = 200,
  label,
}: {
  readonly series: readonly Series[];
  readonly height?: number;
  readonly label: string;
}) {
  const box = { width: 720, height, padding: 8 };
  const extent = extentOf(series);

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
      <svg
        viewBox={`0 0 ${box.width} ${box.height}`}
        className="w-full"
        role="img"
        aria-label={label}
        preserveAspectRatio="none"
      >
        {series.map((s, i) => (
          <path
            key={s.key}
            d={linePath(s.points, extent, box)}
            fill="none"
            stroke={colorFor(s.key, i)}
            strokeWidth={2}
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />
        ))}
      </svg>
      <figcaption className="flex flex-wrap gap-3">
        {series.map((s, i) => (
          <span key={s.key} className="flex items-center gap-1 text-[11px]">
            {/* Never colour alone (TITAN-FRT-044): the swatch is paired with
                the key's text, so the legend survives greyscale. */}
            <span
              className="h-2 w-2 shrink-0 rounded-sm"
              style={{ backgroundColor: colorFor(s.key, i) }}
              aria-hidden
            />
            <span className="font-mono text-muted-foreground">{s.key}</span>
          </span>
        ))}
      </figcaption>
    </figure>
  );
}
