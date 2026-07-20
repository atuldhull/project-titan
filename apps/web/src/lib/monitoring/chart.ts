/**
 * Pure chart maths for Monitoring (Vol 4 §6.10).
 *
 * **Why this is hand-rolled SVG and not Recharts, which §6.10 names.**
 * §6.10 asks for "time-series charts (Recharts, dynamically imported)". The
 * "dynamically imported" half says what the requirement is really protecting:
 * chart code must not land in the shared bundle. Recharts is ~100 kB gzipped
 * plus a d3 tail, and it would buy a line chart, a stacked bar and a sparkline
 * — which are, between them, the ~40 lines of scale-and-path arithmetic below.
 *
 * The deciding argument is testability, and it is the same one the last three
 * phases kept running into: jsdom has no layout, so a rendered chart can never
 * be honestly asserted on here — with Recharts the chart would be untestable
 * full stop. As pure functions, the part that can actually be WRONG (the
 * scales, the empty and single-point cases, the stacking) is tested exactly.
 * The SVG that consumes them holds no judgement at all.
 *
 * The trade-off, stated plainly: no crosshair, no synced tooltips across charts
 * (§6.10 asks for both), and no animation. Those are real losses.
 */

/** A point in data space. `x` is epoch ms; `y` is whatever the series measures. */
export interface Point {
  readonly x: number;
  readonly y: number;
}

export interface Series {
  readonly key: string;
  readonly points: readonly Point[];
}

export interface Extent {
  readonly minX: number;
  readonly maxX: number;
  readonly minY: number;
  readonly maxY: number;
}

export interface Box {
  readonly width: number;
  readonly height: number;
  readonly padding: number;
}

/**
 * The data-space bounds of every series.
 *
 * y always starts at 0: a cost chart whose axis starts at the cheapest point
 * exaggerates every difference above it, which is the classic way to make a
 * truthful chart lie.
 */
export function extentOf(series: readonly Series[]): Extent | null {
  const points = series.flatMap((s) => s.points);
  if (points.length === 0) return null;

  let minX = Infinity;
  let maxX = -Infinity;
  let maxY = 0;
  for (const p of points) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  return { minX, maxX, minY: 0, maxY };
}

/**
 * Project a data point into SVG space.
 *
 * A zero-width extent (one point, or every point at the same instant) would
 * divide by zero and put the point at NaN — which renders as an invisible,
 * silent nothing. It is centred instead.
 */
export function project(
  point: Point,
  extent: Extent,
  box: Box,
): { x: number; y: number } {
  const spanX = extent.maxX - extent.minX;
  const spanY = extent.maxY - extent.minY;
  const inner = {
    w: Math.max(0, box.width - box.padding * 2),
    h: Math.max(0, box.height - box.padding * 2),
  };
  const fx = spanX === 0 ? 0.5 : (point.x - extent.minX) / spanX;
  const fy = spanY === 0 ? 0 : (point.y - extent.minY) / spanY;
  return {
    x: box.padding + fx * inner.w,
    // SVG's y grows downward; data's grows upward.
    y: box.padding + inner.h - fy * inner.h,
  };
}

/** An SVG path for one series. "" when there is nothing to draw. */
export function linePath(
  points: readonly Point[],
  extent: Extent,
  box: Box,
): string {
  if (points.length === 0) return "";
  const projected = points.map((p) => project(p, extent, box));
  if (projected.length === 1) {
    // A single point has no line. Draw a short horizontal tick so the series
    // is visible at all — a lone `M` command paints nothing.
    const only = projected[0];
    return `M ${round(only.x - 2)} ${round(only.y)} L ${round(only.x + 2)} ${round(only.y)}`;
  }
  return projected
    .map((p, i) => `${i === 0 ? "M" : "L"} ${round(p.x)} ${round(p.y)}`)
    .join(" ");
}

/** The same path, closed to the baseline — a filled sparkline. */
export function areaPath(
  points: readonly Point[],
  extent: Extent,
  box: Box,
): string {
  const line = linePath(points, extent, box);
  if (!line || points.length < 2) return "";
  const first = project(points[0], extent, box);
  const last = project(points[points.length - 1], extent, box);
  const baseline = box.height - box.padding;
  return `${line} L ${round(last.x)} ${round(baseline)} L ${round(first.x)} ${round(baseline)} Z`;
}

function round(n: number): number {
  // Two decimals is well under a device pixel and keeps the DOM small; a raw
  // float here would put 15 digits into every path command.
  return Math.round(n * 100) / 100;
}

/**
 * Align many groups' points onto one x-axis.
 *
 * The backend emits no row for an empty bucket (`date_trunc`, not a generated
 * series), so two providers active at different times produce points at
 * different x's. A stacked chart has to agree on the x-axis, so the union of
 * every x is filled with explicit zeros.
 */
export function alignSeries(series: readonly Series[]): Series[] {
  const xs = Array.from(
    new Set(series.flatMap((s) => s.points.map((p) => p.x))),
  ).sort((a, b) => a - b);
  return series.map((s) => {
    const byX = new Map(s.points.map((p) => [p.x, p.y]));
    return { key: s.key, points: xs.map((x) => ({ x, y: byX.get(x) ?? 0 })) };
  });
}

/** Cumulative stacking: each series sits on the sum of those before it. */
export function stack(series: readonly Series[]): Series[] {
  const aligned = alignSeries(series);
  const running = new Map<number, number>();
  return aligned.map((s) => ({
    key: s.key,
    points: s.points.map((p) => {
      const base = running.get(p.x) ?? 0;
      const top = base + p.y;
      running.set(p.x, top);
      return { x: p.x, y: top };
    }),
  }));
}

/**
 * Deterministic colours, so a series keeps its colour between renders.
 *
 * CSS variables, not literals: TITAN-FRT-010 forbids raw colours in app code,
 * and these genuinely differ per theme — the same hue that reads on white is
 * muddy on the dark surface. Defined in globals.css beside the other tokens.
 */
const PALETTE = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
] as const;

export function colorFor(key: string, index: number): string {
  // By index, not by hashing the key: a hash spreads nicely but can collide,
  // and two adjacent series in the same colour is worse than a repeat at
  // position 7.
  return PALETTE[index % PALETTE.length];
}

/**
 * Stroke dash patterns, paired with `colorFor` (TITAN-FRT-044).
 *
 * Two separate requirements land on this one array, and only the second is
 * obvious:
 *
 * 1. **-044 forbids colour-only encoding.** A stacked chart whose series differ
 *    ONLY by stroke colour is unreadable in greyscale and to a dichromat — the
 *    legend says "anthropic is blue" and the reader cannot tell blue from
 *    green. The dash is the second, non-colour channel, and the legend mirrors
 *    it (see `sparkline.tsx`) so the pairing is learnable.
 * 2. **The palette runs out.** `usage.data.groups` is server-driven and
 *    unbounded — group-by `model` on a busy stack easily exceeds six — and
 *    `colorFor` wraps, so series 7 would be drawn exactly like series 1.
 *
 * FIVE patterns against SIX colours is deliberate: the lengths are coprime, so
 * (colour, dash) is unique for the first **30** series rather than repeating at
 * 6 (which `Math.floor(index / 6)` would give) or at 5. Past 30 it does repeat;
 * a chart with 31 series is illegible for reasons no palette can fix, and the
 * backend's `truncated` flag already trims the worst of it.
 */
const DASHES: readonly (string | undefined)[] = [
  undefined, // solid
  "7 4", // dashed
  "1.5 3", // dotted
  "11 4 2 4", // dash-dot
  "3 3", // short dash
] as const;

/**
 * The dash pattern for a series position, or `undefined` for a solid line.
 *
 * `undefined` rather than `"none"` so React omits `stroke-dasharray` entirely —
 * `"none"` is legal but puts a redundant attribute on every solid path, and the
 * legend swatch below reuses this same value.
 */
export function dashFor(index: number): string | undefined {
  return DASHES[index % DASHES.length];
}

/** How many series get a unique (colour, dash) pair before the scheme repeats. */
export const DISTINCT_SERIES = PALETTE.length * DASHES.length;

/*
 * `thresholdLevel(value, {warn, critical})` used to live here, for §6.10's
 * "threshold colouring". It was deleted in Phase 65 rather than wired, for two
 * reasons:
 *
 *  - It had zero callers and never had any. §6.10's thresholds belong to the
 *    golden signals (API p95, error rate, queue depth), and those have no
 *    backend at all — see the header of `monitoring-screen.tsx`. The one number
 *    on this screen with a ceiling is LLM spend, but the ceiling
 *    (`budgets.per_project_usd`) is per-PROJECT while these totals are org-wide,
 *    so comparing them would be arithmetic that means nothing.
 *  - Its whole output was a colour bucket, which is the shape TITAN-FRT-044
 *    exists to discourage. Anything that reintroduces threshold state must pair
 *    it with an icon or a word, so it would not have been reusable as written.
 */
