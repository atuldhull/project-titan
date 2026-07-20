/**
 * The chart maths (Vol 4 §6.10).
 *
 * This is the whole reason the charts are hand-rolled rather than Recharts:
 * jsdom has no layout, so a rendered chart can never be honestly asserted on —
 * with a chart library the scales, the empty cases and the stacking would be
 * untestable. As pure functions they are exactly testable.
 */
import { describe, expect, it } from "vitest";

import {
  alignSeries,
  areaPath,
  colorFor,
  dashFor,
  DISTINCT_SERIES,
  extentOf,
  linePath,
  project,
  stack,
  type Series,
} from "@/lib/monitoring/chart";

const BOX = { width: 100, height: 50, padding: 0 };

describe("extentOf", () => {
  it("is null when there is nothing to draw", () => {
    expect(extentOf([])).toBeNull();
    expect(extentOf([{ key: "a", points: [] }])).toBeNull();
  });

  it("spans every series", () => {
    const series: Series[] = [
      { key: "a", points: [{ x: 10, y: 5 }] },
      { key: "b", points: [{ x: 30, y: 1 }] },
    ];
    expect(extentOf(series)).toEqual({ minX: 10, maxX: 30, minY: 0, maxY: 5 });
  });

  it("always starts y at zero", () => {
    // A cost axis starting at the cheapest point exaggerates every difference
    // above it — the classic way to make a truthful chart lie.
    const extent = extentOf([
      {
        key: "a",
        points: [
          { x: 0, y: 100 },
          { x: 1, y: 101 },
        ],
      },
    ]);
    expect(extent?.minY).toBe(0);
  });
});

describe("project", () => {
  const extent = { minX: 0, maxX: 10, minY: 0, maxY: 10 };

  it("puts the origin at the bottom-left", () => {
    // SVG's y grows downward; data's grows upward.
    expect(project({ x: 0, y: 0 }, extent, BOX)).toEqual({ x: 0, y: 50 });
  });

  it("puts the maximum at the top-right", () => {
    expect(project({ x: 10, y: 10 }, extent, BOX)).toEqual({ x: 100, y: 0 });
  });

  it("centres a zero-width extent rather than dividing by zero", () => {
    // One point, or every point at the same instant. NaN here would render as
    // an invisible, silent nothing.
    const flat = { minX: 5, maxX: 5, minY: 0, maxY: 0 };
    const at = project({ x: 5, y: 0 }, flat, BOX);
    expect(Number.isNaN(at.x)).toBe(false);
    expect(Number.isNaN(at.y)).toBe(false);
    expect(at.x).toBe(50);
  });
});

describe("linePath", () => {
  const extent = { minX: 0, maxX: 10, minY: 0, maxY: 10 };

  it("is empty for no points", () => {
    expect(linePath([], extent, BOX)).toBe("");
  });

  it("draws a visible tick for a single point", () => {
    // A lone `M` command paints nothing at all, so a one-bucket range would
    // render as a blank chart that looks broken.
    const path = linePath([{ x: 5, y: 5 }], extent, BOX);
    expect(path).toContain("M");
    expect(path).toContain("L");
  });

  it("moves once then lines", () => {
    const path = linePath(
      [
        { x: 0, y: 0 },
        { x: 10, y: 10 },
      ],
      extent,
      BOX,
    );
    expect(path.match(/M/g)).toHaveLength(1);
    expect(path.match(/L/g)).toHaveLength(1);
  });

  it("rounds coordinates rather than emitting raw floats", () => {
    // The extent must divide UNEVENLY into the box, or the projection lands on
    // whole numbers and `round()` is invisible — the first draft used a 0..10
    // extent on a 100px box, so every point was already integral and the test
    // passed with round() deleted. A span of 3 gives 1/3 = 33.333…, which only
    // rounding tames.
    const thirds = { minX: 0, maxX: 3, minY: 0, maxY: 3 };
    const path = linePath(
      [
        { x: 1, y: 1 },
        { x: 2, y: 2 },
      ],
      thirds,
      BOX,
    );
    expect(path).toContain("33.33"); // the rounded value is actually present
    expect(path).not.toMatch(/\d\.\d{3,}/); // and no raw 15-digit float
  });
});

describe("areaPath", () => {
  const extent = { minX: 0, maxX: 10, minY: 0, maxY: 10 };

  it("closes back to the baseline", () => {
    const path = areaPath(
      [
        { x: 0, y: 5 },
        { x: 10, y: 5 },
      ],
      extent,
      BOX,
    );
    expect(path.endsWith("Z")).toBe(true);
  });

  it("is empty for fewer than two points, since there is no area", () => {
    expect(areaPath([{ x: 1, y: 1 }], extent, BOX)).toBe("");
    expect(areaPath([], extent, BOX)).toBe("");
  });
});

describe("alignSeries", () => {
  it("fills a missing bucket with an explicit zero", () => {
    // The backend emits NO row for an empty bucket (date_trunc, not a
    // generated series), so two providers active at different times arrive
    // with different x's — and a stacked chart must agree on one axis.
    const aligned = alignSeries([
      { key: "a", points: [{ x: 1, y: 10 }] },
      { key: "b", points: [{ x: 2, y: 20 }] },
    ]);
    expect(aligned[0].points).toEqual([
      { x: 1, y: 10 },
      { x: 2, y: 0 },
    ]);
    expect(aligned[1].points).toEqual([
      { x: 1, y: 0 },
      { x: 2, y: 20 },
    ]);
  });

  it("sorts the shared axis", () => {
    const aligned = alignSeries([
      {
        key: "a",
        points: [
          { x: 9, y: 1 },
          { x: 2, y: 1 },
        ],
      },
    ]);
    expect(aligned[0].points.map((p) => p.x)).toEqual([2, 9]);
  });
});

describe("stack", () => {
  it("puts each series on the sum of those before it", () => {
    const stacked = stack([
      { key: "a", points: [{ x: 1, y: 2 }] },
      { key: "b", points: [{ x: 1, y: 3 }] },
    ]);
    expect(stacked[0].points[0].y).toBe(2);
    expect(stacked[1].points[0].y).toBe(5);
  });

  it("stacks across a bucket one series is missing from", () => {
    const stacked = stack([
      { key: "a", points: [{ x: 1, y: 2 }] },
      {
        key: "b",
        points: [
          { x: 1, y: 3 },
          { x: 2, y: 4 },
        ],
      },
    ]);
    // At x=2, `a` contributes an aligned zero, so `b` sits at 4, not 6.
    expect(stacked[1].points.find((p) => p.x === 2)?.y).toBe(4);
  });
});

describe("colorFor", () => {
  it("is stable for a given position", () => {
    expect(colorFor("anthropic", 0)).toBe(colorFor("openai", 0));
    expect(colorFor("anthropic", 0)).not.toBe(colorFor("anthropic", 1));
  });

  it("wraps rather than running out", () => {
    expect(colorFor("x", 99)).toBeTruthy();
  });
});

/**
 * `thresholdLevel` was deleted in Phase 65 and its tests with it — it had zero
 * callers, the golden signals it was written for have no backend at all, and its
 * only output was a colour bucket, which is the shape TITAN-FRT-044 exists to
 * discourage. The reasoning is recorded in `chart.ts` where it used to live.
 */
describe("dashFor", () => {
  it("gives each of the first six series a distinguishing dash", () => {
    // TITAN-FRT-044: the stacked chart's series differed ONLY by stroke colour,
    // which is unreadable in greyscale and to a dichromat. The dash is the
    // second, non-colour channel — so within one screenful of series it must
    // actually vary, not merely exist.
    const first = [0, 1, 2, 3, 4].map(dashFor);
    expect(new Set(first).size).toBe(5);
  });

  it("is undefined for the first series, so solid paths carry no attribute", () => {
    expect(dashFor(0)).toBeUndefined();
  });

  it("keeps (colour, dash) unique past the six-colour palette", () => {
    // The real bug this guards: `usage.data.groups` is server-driven and
    // unbounded, `colorFor` wraps at 6, so series 7 was drawn EXACTLY like
    // series 1. Pairing is what disambiguates it — and only because 5 and 6 are
    // coprime. A 6-long dash array would repeat at 6 alongside the colours and
    // fix nothing, which is the mistake this asserts against.
    const pairs = Array.from(
      { length: DISTINCT_SERIES },
      (_, i) => `${colorFor("k", i)}|${dashFor(i) ?? "solid"}`,
    );
    expect(new Set(pairs).size).toBe(DISTINCT_SERIES);
    expect(DISTINCT_SERIES).toBe(30);

    // Specifically: 7 must not look like 1.
    expect(colorFor("k", 6)).toBe(colorFor("k", 0)); // colour DOES wrap …
    expect(dashFor(6)).not.toBe(dashFor(0)); // … and the dash is why that is ok
  });

  it("repeats only after the full cycle", () => {
    const cycle = (i: number) => `${colorFor("k", i)}|${dashFor(i) ?? "solid"}`;
    expect(cycle(DISTINCT_SERIES)).toBe(cycle(0));
  });
});
