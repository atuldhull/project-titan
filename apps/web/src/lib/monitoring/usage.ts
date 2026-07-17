/**
 * Turning the usage ledger into what §6.10 draws.
 *
 * Pure: the endpoint's shape in, chart series and tile values out.
 */
import type { Series } from "@/lib/monitoring/chart";

/** §6.10's range picker, and the bucket each range asks the backend for. */
export const RANGES = {
  "15m": { ms: 15 * 60_000, bucket: "minute" },
  "1h": { ms: 60 * 60_000, bucket: "minute" },
  "24h": { ms: 24 * 60 * 60_000, bucket: "hour" },
  "7d": { ms: 7 * 24 * 60 * 60_000, bucket: "hour" },
} as const;

export type RangeKey = keyof typeof RANGES;

export function isRangeKey(value: string): value is RangeKey {
  return value in RANGES;
}

/** §6.10's group-by toggle (FRT-033: "per agent and per project"). */
export const GROUP_BYS = [
  "provider",
  "model",
  "agent",
  "project",
  "alias",
] as const;
export type GroupBy = (typeof GROUP_BYS)[number];

export function isGroupBy(value: string): value is GroupBy {
  return (GROUP_BYS as readonly string[]).includes(value);
}

interface UsagePointLike {
  readonly bucket_at: string;
  readonly key?: string | null;
  readonly cost_usd: string;
  readonly input_tokens: number;
  readonly output_tokens: number;
}

/**
 * Cost is a STRING on the wire because it is `Numeric(14,6)`, and it becomes a
 * number exactly once — here, at the point of drawing.
 *
 * That is safe and the string is not paranoia: a chart's y-axis is a float
 * regardless, but the TOTALS a human reads are asserted to the micro-dollar
 * against the ledger, and a total assembled by adding floats drifts. Sums stay
 * on the server; this only ever converts a value that is about to be a pixel.
 */
export function costOf(point: { readonly cost_usd: string }): number {
  return Number(point.cost_usd);
}

/** Group the flat `series` rows into one Series per group key. */
export function toSeries(
  points: readonly UsagePointLike[],
  metric: "cost" | "tokens" = "cost",
): Series[] {
  const byKey = new Map<string, { x: number; y: number }[]>();
  for (const p of points) {
    // A null key is real and common: most of a default stack's ledger has no
    // project and no agent bound. It is a group, not a gap.
    const key = p.key ?? "(none)";
    const y = metric === "cost" ? costOf(p) : p.input_tokens + p.output_tokens;
    const at = Date.parse(p.bucket_at);
    if (Number.isNaN(at)) continue;
    const list = byKey.get(key);
    if (list) list.push({ x: at, y });
    else byKey.set(key, [{ x: at, y }]);
  }
  return Array.from(byKey, ([key, points]) => ({
    key,
    points: points.sort((a, b) => a.x - b.x),
  })).sort((a, b) => a.key.localeCompare(b.key));
}

/** `$1.87`, `$0.0042`, `—`. */
export function formatUsd(value: number): string {
  if (!Number.isFinite(value)) return "—";
  if (value === 0) return "$0.00";
  // Sub-cent spend is the normal case on a local stack, and "$0.00" for a real
  // charge reads as "free" rather than "small". But `toPrecision(2)` emits
  // SCIENTIFIC NOTATION below ~1e-4 ("$8.3e-7"), which is unreadable in a money
  // tile — and the spend/hr projection on a quiet 24h range lands exactly
  // there. So fixed-decimal, trimmed, with a floor below which we say
  // "< $0.000001" rather than a string of zeros.
  if (value < 0.01) {
    if (value < 0.000001) return "< $0.000001";
    return `$${value.toFixed(6).replace(/0+$/, "").replace(/\.$/, "")}`;
  }
  return `$${value.toFixed(2)}`;
}

export function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

/**
 * Spend per hour over the range — §6.10's "LLM spend/hr" tile.
 *
 * Extrapolated from the window rather than measured: there is no per-hour
 * counter, so a 15-minute window showing $0.50 reports $2.00/hr. Which is a
 * projection, and the tile says "/hr" precisely so it reads as a rate.
 */
export function spendPerHour(totalUsd: number, rangeMs: number): number {
  if (rangeMs <= 0) return 0;
  return totalUsd * (3_600_000 / rangeMs);
}

export function rangeStart(range: RangeKey, nowMs: number): Date {
  return new Date(nowMs - RANGES[range].ms);
}
