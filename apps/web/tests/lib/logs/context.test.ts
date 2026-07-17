/**
 * The ±50-line window behind §6.7's ERROR context drawer.
 */
import { describe, expect, it } from "vitest";

import type { LogLine } from "@titan/api-client";

import { contextOffset, contextWindow } from "@/lib/logs/context";
import { appendLines, EMPTY_RING } from "@/lib/logs/ring";

const BASE = BigInt("1700000000000000000");

function line(i: number): LogLine {
  const ts = String(BASE + BigInt(i) * BigInt(1_000_000));
  return {
    id: `l${i}`,
    ts_ns: ts,
    line: `line ${i}`,
    labels: {},
    level: "info",
    event: `line ${i}`,
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

const ring = appendLines(
  EMPTY_RING,
  Array.from({ length: 200 }, (_, i) => line(i)),
);

describe("contextWindow", () => {
  it("returns the target plus 50 either side", () => {
    const window = contextWindow(ring, "l100");
    expect(window).toHaveLength(101);
    expect(window[0].id).toBe("l50");
    expect(window[100].id).toBe("l150");
  });

  it("does NOT slide the window near the start of the buffer", () => {
    // A line 3 from the start has three lines before it. Showing 97 after it
    // and calling that "±50" would misrepresent where the line sits.
    const window = contextWindow(ring, "l3");
    expect(window[0].id).toBe("l0");
    expect(window).toHaveLength(54); // 3 before + itself + 50 after
  });

  it("does not slide near the end either", () => {
    const window = contextWindow(ring, "l197");
    expect(window[window.length - 1].id).toBe("l199");
    expect(window).toHaveLength(53); // 50 before + itself + 2 after
  });

  it("is empty for a line that is not in the buffer", () => {
    // The buffer is capped, so the line a drawer was opened on can be evicted
    // by the tail while the drawer is open.
    expect(contextWindow(ring, "nope")).toEqual([]);
  });

  it("handles a buffer smaller than the radius", () => {
    const small = appendLines(EMPTY_RING, [line(0), line(1)]);
    expect(contextWindow(small, "l0")).toHaveLength(2);
  });
});

describe("contextOffset", () => {
  it("locates the target inside the returned window", () => {
    expect(contextOffset(ring, "l100")).toBe(50);
    expect(contextOffset(ring, "l3")).toBe(3); // clamped at the start
  });

  it("is -1 for a line that is not held", () => {
    expect(contextOffset(ring, "nope")).toBe(-1);
  });
});
