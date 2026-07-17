/**
 * The 50k ring buffer (ADR-FRT-05).
 *
 * This is the tested surface for the viewer's data handling. The virtualized
 * list itself cannot be tested here: react-virtual v3.14.6 bails out when
 * `outerSize === 0`, and jsdom has no layout, so `getVirtualItems()` returns []
 * and a rendered list has ZERO rows regardless of how many lines it was given.
 * A test asserting on rendered rows would fail; one asserting emptiness would
 * pass while proving nothing. So the decisions live here, in the pure core.
 */
import { describe, expect, it } from "vitest";

import type { LogLine } from "@titan/api-client";

import {
  appendLines,
  compareTs,
  EMPTY_RING,
  isSaturated,
  mergeLines,
  newest,
  oldest,
  prependLines,
} from "@/lib/logs/ring";

const BASE = BigInt("1700000000000000000");

function line(offsetNs: number | bigint, text = "x"): LogLine {
  const ts = String(BASE + BigInt(offsetNs));
  return {
    id: `${ts}:${text}`,
    ts_ns: ts,
    line: text,
    labels: { service: "api" },
    level: "info",
    event: text,
    module: null,
    request_id: null,
    trace_id: null,
    span_id: null,
    project_id: null,
    task_id: null,
    agent_id: null,
    fields: {},
  } as LogLine;
}

describe("compareTs", () => {
  it("orders nanoseconds that a Number would collapse", () => {
    // Both of these round to the same f64: they differ below 2**53's precision.
    const a = "1700000000123456789";
    const b = "1700000000123456790";
    expect(Number(a) === Number(b)).toBe(true); // the bug, if we used numbers
    expect(compareTs(a, b)).toBe(-1); // BigInt sees the difference
  });

  it("is a proper comparator", () => {
    expect(compareTs("1", "2")).toBe(-1);
    expect(compareTs("2", "1")).toBe(1);
    expect(compareTs("2", "2")).toBe(0);
  });
});

describe("appendLines", () => {
  it("appends in order", () => {
    const ring = appendLines(EMPTY_RING, [line(1, "a"), line(2, "b")]);
    expect(ring.map((l) => l.line)).toEqual(["a", "b"]);
  });

  it("drops ids already held", () => {
    const first = appendLines(EMPTY_RING, [line(1, "a")]);
    const second = appendLines(first, [line(1, "a"), line(2, "b")]);
    expect(second.map((l) => l.line)).toEqual(["a", "b"]);
  });

  it("drops duplicates WITHIN one batch", () => {
    // A gap fill and a tail poll can both carry a line, and they can land in
    // the same rAF flush — so the batch itself is not guaranteed unique.
    const ring = appendLines(EMPTY_RING, [
      line(1, "a"),
      line(1, "a"),
      line(2, "b"),
    ]);
    expect(ring).toHaveLength(2);
  });

  it("returns the SAME array when nothing is added", () => {
    // Identity is what lets React skip a render for an all-duplicate batch (a
    // poll that raced the tail) and keeps the filter memo from recomputing.
    const ring = appendLines(EMPTY_RING, [line(1, "a")]);
    expect(appendLines(ring, [line(1, "a")])).toBe(ring);
    expect(appendLines(ring, [])).toBe(ring);
  });

  it("evicts the OLDEST beyond capacity", () => {
    const ring = appendLines(
      EMPTY_RING,
      [line(1, "a"), line(2, "b"), line(3, "c")],
      2,
    );
    expect(ring.map((l) => l.line)).toEqual(["b", "c"]);
  });

  it("holds exactly the capacity under sustained append", () => {
    let ring = EMPTY_RING;
    for (let i = 0; i < 500; i++)
      ring = appendLines(ring, [line(i, `l${i}`)], 100);
    expect(ring).toHaveLength(100);
    expect(newest(ring)?.line).toBe("l499");
    expect(oldest(ring)?.line).toBe("l400");
  });
});

describe("prependLines", () => {
  it("splices older lines onto the front", () => {
    const ring = appendLines(EMPTY_RING, [line(5, "e")]);
    expect(
      prependLines(ring, [line(3, "c"), line(4, "d")]).map((l) => l.line),
    ).toEqual(["c", "d", "e"]);
  });

  it("evicts the NEWEST beyond capacity", () => {
    // Backwards-looking on purpose: the reader scrolled INTO the past, so the
    // past is what they are reading. Dropping what they just asked for to keep
    // lines they scrolled away from would make the fetch pointless.
    const ring = appendLines(EMPTY_RING, [line(4, "d"), line(5, "e")]);
    expect(prependLines(ring, [line(3, "c")], 2).map((l) => l.line)).toEqual([
      "c",
      "d",
    ]);
  });

  it("returns the SAME array when every line is already held", () => {
    const ring = appendLines(EMPTY_RING, [line(1, "a")]);
    expect(prependLines(ring, [line(1, "a")])).toBe(ring);
  });
});

describe("mergeLines", () => {
  it("restores order for a splice landing in the middle", () => {
    // What a gap fill does once the reader has scrolled past the gap, so lines
    // exist on both sides of it.
    const ring = appendLines(EMPTY_RING, [line(1, "a"), line(9, "z")]);
    expect(mergeLines(ring, [line(5, "m")]).map((l) => l.line)).toEqual([
      "a",
      "m",
      "z",
    ]);
  });

  it("does not repeat a line that is already held", () => {
    const ring = appendLines(EMPTY_RING, [line(1, "a"), line(5, "m")]);
    expect(mergeLines(ring, [line(5, "m")])).toBe(ring);
  });

  it("sorts by nanosecond, not by the rounded number", () => {
    const near = (suffix: string): LogLine => {
      const l = line(0, suffix);
      return { ...l, ts_ns: `170000000012345678${suffix}`, id: `id-${suffix}` };
    };
    const merged = mergeLines(EMPTY_RING, [near("9"), near("1"), near("5")]);
    expect(merged.map((l) => l.ts_ns)).toEqual([
      "1700000000123456781",
      "1700000000123456785",
      "1700000000123456789",
    ]);
  });

  it("keeps the newest when over capacity", () => {
    const merged = mergeLines(
      EMPTY_RING,
      [line(1, "a"), line(2, "b"), line(3, "c")],
      2,
    );
    expect(merged.map((l) => l.line)).toEqual(["b", "c"]);
  });
});

describe("newest / oldest / isSaturated", () => {
  it("are null on an empty ring", () => {
    expect(newest(EMPTY_RING)).toBeNull();
    expect(oldest(EMPTY_RING)).toBeNull();
  });

  it("reports saturation at capacity", () => {
    const ring = appendLines(EMPTY_RING, [line(1), line(2)], 2);
    expect(isSaturated(ring, 2)).toBe(true);
    expect(isSaturated(appendLines(EMPTY_RING, [line(1)]), 2)).toBe(false);
  });
});
