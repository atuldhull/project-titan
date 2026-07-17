/**
 * Ledger -> chart (Vol 4 §6.10, TITAN-FRT-033).
 */
import { describe, expect, it } from "vitest";

import {
  costOf,
  formatTokens,
  formatUsd,
  isGroupBy,
  isRangeKey,
  RANGES,
  rangeStart,
  spendPerHour,
  toSeries,
} from "@/lib/monitoring/usage";

const point = (over: Partial<Parameters<typeof toSeries>[0][number]> = {}) => ({
  bucket_at: "2026-07-16T12:00:00Z",
  key: "anthropic",
  cost_usd: "1.500000",
  input_tokens: 10,
  output_tokens: 5,
  ...over,
});

describe("costOf", () => {
  it("parses the wire string", () => {
    expect(costOf({ cost_usd: "1.123457" })).toBeCloseTo(1.123457, 6);
  });

  it("keeps the string exact until it is drawn", () => {
    // The wire carries a string because `Numeric(14,6)` totals are asserted to
    // the micro-dollar; this converts one value on its way to being a pixel,
    // and never sums on the client.
    expect(costOf({ cost_usd: "0.000001" })).toBe(0.000001);
  });
});

describe("toSeries", () => {
  it("groups points by key and sorts them in time", () => {
    const series = toSeries([
      point({ bucket_at: "2026-07-16T13:00:00Z", key: "a", cost_usd: "2" }),
      point({ bucket_at: "2026-07-16T12:00:00Z", key: "a", cost_usd: "1" }),
      point({ key: "b", cost_usd: "5" }),
    ]);
    expect(series.map((s) => s.key)).toEqual(["a", "b"]);
    expect(series[0].points.map((p) => p.y)).toEqual([1, 2]);
  });

  it("keeps a null key as a real group, not a gap", () => {
    // On a default stack this is MOST of the ledger: the live executor and CEO
    // intake are off, so the only writer is memory-ingest, which binds no
    // project and no agent. Dropping it would empty the chart.
    const series = toSeries([point({ key: null })]);
    expect(series[0].key).toBe("(none)");
  });

  it("sums both token directions for the tokens metric", () => {
    const series = toSeries(
      [point({ input_tokens: 10, output_tokens: 5 })],
      "tokens",
    );
    expect(series[0].points[0].y).toBe(15);
  });

  it("skips a point whose timestamp will not parse", () => {
    // NaN on the x-axis silently collapses the whole chart's extent.
    expect(toSeries([point({ bucket_at: "not-a-date" })])).toEqual([]);
  });

  it("is empty for no points", () => {
    expect(toSeries([])).toEqual([]);
  });
});

describe("spendPerHour", () => {
  it("extrapolates the window to an hour", () => {
    // There is no per-hour counter; the tile says "/hr" so it reads as a rate.
    expect(spendPerHour(0.5, RANGES["15m"].ms)).toBeCloseTo(2, 6);
    expect(spendPerHour(3, RANGES["1h"].ms)).toBeCloseTo(3, 6);
  });

  it("is zero rather than Infinity for an empty window", () => {
    expect(spendPerHour(1, 0)).toBe(0);
  });
});

describe("formatUsd", () => {
  it("shows sub-cent spend rather than rounding it to free", () => {
    // A local stack's whole ledger is often fractions of a cent; "$0.00" for a
    // real charge reads as "free" rather than "small".
    expect(formatUsd(0.0042)).toBe("$0.0042");
    expect(formatUsd(0)).toBe("$0.00");
  });

  it("never renders scientific notation, however tiny", () => {
    // `toPrecision(2)` emits "$8.3e-7" below ~1e-4, which is unreadable in a
    // money tile — and the spend/hr projection on a quiet 24h range lands
    // exactly there.
    expect(formatUsd(0.0000008)).not.toContain("e");
    expect(formatUsd(0.0000008)).toBe("< $0.000001");
    expect(formatUsd(0.000012)).toBe("$0.000012");
  });

  it("shows ordinary money to the cent", () => {
    expect(formatUsd(1.874)).toBe("$1.87");
  });

  it("does not render a non-finite value as NaN", () => {
    expect(formatUsd(Number.NaN)).toBe("—");
  });
});

describe("formatTokens", () => {
  it("abbreviates at thousands and millions", () => {
    expect(formatTokens(999)).toBe("999");
    expect(formatTokens(1500)).toBe("1.5k");
    expect(formatTokens(2_400_000)).toBe("2.4M");
  });
});

describe("ranges", () => {
  it("accepts only §6.10's four ranges", () => {
    expect(isRangeKey("15m")).toBe(true);
    expect(isRangeKey("7d")).toBe(true);
    expect(isRangeKey("30d")).toBe(false);
  });

  it("picks a bucket that keeps the chart readable", () => {
    // Not one point per pixel and not one per week.
    expect(RANGES["15m"].bucket).toBe("minute");
    expect(RANGES["7d"].bucket).toBe("hour");
  });

  it("computes the start from the range", () => {
    const now = Date.parse("2026-07-16T12:00:00Z");
    expect(rangeStart("1h", now).toISOString()).toBe(
      "2026-07-16T11:00:00.000Z",
    );
  });

  it("accepts only the group-bys the backend allows", () => {
    expect(isGroupBy("provider")).toBe(true);
    expect(isGroupBy("agent")).toBe(true);
    // The backend interpolates this into a GROUP BY behind an allowlist; a
    // value it does not know is a 422, so the client must not offer one.
    expect(isGroupBy("provider; DROP TABLE")).toBe(false);
  });
});
