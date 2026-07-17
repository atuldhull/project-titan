/**
 * Coverage gaps and the row model (§6.7's "· N lines skipped [load] ·").
 */
import { describe, expect, it } from "vitest";

import type { LogLine } from "@titan/api-client";

import {
  addGap,
  buildRows,
  failGap,
  fillGap,
  gapDurationMs,
  loadingGap,
  openGap,
  shouldJump,
  updateGap,
} from "@/lib/logs/gaps";
import { EMPTY_RING } from "@/lib/logs/ring";

const BASE = BigInt("1700000000000000000");
const ns = (offsetMs: number): string =>
  String(BASE + BigInt(offsetMs) * BigInt(1_000_000));

function line(offsetMs: number, text = "x"): LogLine {
  return {
    id: `${ns(offsetMs)}:${text}`,
    ts_ns: ns(offsetMs),
    line: text,
    labels: {},
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

describe("openGap", () => {
  it("is identified by its bounds, so re-jumping does not duplicate it", () => {
    expect(openGap(ns(0), ns(10)).id).toBe(openGap(ns(0), ns(10)).id);
    expect(openGap(ns(0), ns(10)).id).not.toBe(openGap(ns(0), ns(20)).id);
  });

  it("starts idle, unfilled and open", () => {
    const gap = openGap(ns(0), ns(10));
    expect(gap.status).toBe("idle");
    expect(gap.filled).toBe(0);
    expect(gap.complete).toBe(false);
  });
});

describe("gapDurationMs", () => {
  it("reports the window the marker displays", () => {
    // The marker names the time range, not a line count: counting would need a
    // second (metric) query over the same range, so a number here would be
    // invented. The window we genuinely know.
    expect(gapDurationMs(openGap(ns(0), ns(2_500)))).toBe(2_500);
  });

  it("does not overflow on a nanosecond that a Number would round", () => {
    expect(
      gapDurationMs(openGap("1700000000000000000", "1700000060000000000")),
    ).toBe(60_000);
  });
});

describe("shouldJump", () => {
  it("catches up rather than jumping after a brief pause", () => {
    // A jump here would open a marker over lines the very next poll delivers.
    expect(shouldJump(1_000_000, 1_005_000)).toBe(false);
  });

  it("jumps when the viewer was away long enough to build a backlog", () => {
    expect(shouldJump(1_000_000, 1_120_000)).toBe(true);
  });

  it("never jumps before the tail has ever run", () => {
    expect(shouldJump(null, Date.now())).toBe(false);
  });

  it("does NOT jump merely because the logs went quiet", () => {
    // THE bug this signature exists to prevent. It takes when the TAIL last
    // succeeded, not how old the newest LINE is. An idle system has an
    // hours-old newest line while the tail is perfectly current, so measuring
    // the line marks every quiet project as a gap and litters the viewer with
    // markers over windows that contain nothing at all.
    const polledHalfASecondAgo = 5_000_000;
    expect(shouldJump(polledHalfASecondAgo, 5_000_500)).toBe(false);
  });
});

describe("fill lifecycle", () => {
  it("marks a gap complete only when the backend offered no cursor", () => {
    // A full page with a cursor means there is MORE in the window — closing the
    // marker there would claim coverage we do not have.
    const gap = openGap(ns(0), ns(10));
    expect(fillGap(gap, 500, "cursor-1").complete).toBe(false);
    expect(fillGap(gap, 12, null).complete).toBe(true);
  });

  it("accumulates what successive fills recovered", () => {
    const gap = fillGap(openGap(ns(0), ns(10)), 500, "cursor-1");
    expect(fillGap(gap, 20, null).filled).toBe(520);
  });

  it("carries the cursor so the next [load] resumes instead of restarting", () => {
    // Without it, a gap wider than one page can never be filled: every click
    // re-requests the same first page, the ring de-duplicates it away, and the
    // only visible effect is `filled` climbing by 500 while the marker stays.
    const gap = fillGap(openGap(ns(0), ns(10)), 500, "cursor-1");
    expect(gap.cursor).toBe("cursor-1");
    expect(gap.complete).toBe(false);
    expect(fillGap(gap, 20, null).cursor).toBeNull();
  });

  it("clears a previous error when a retry succeeds", () => {
    const failed = failGap(openGap(ns(0), ns(10)), "network");
    expect(failed.status).toBe("error");
    expect(fillGap(failed, 5, null).error).toBeUndefined();
  });

  it("clears the error when a retry starts", () => {
    const failed = failGap(openGap(ns(0), ns(10)), "network");
    expect(loadingGap(failed).error).toBeUndefined();
    expect(loadingGap(failed).status).toBe("loading");
  });
});

describe("gap list", () => {
  it("does not add the same gap twice", () => {
    const gap = openGap(ns(0), ns(10));
    expect(addGap(addGap([], gap), gap)).toHaveLength(1);
  });

  it("removes a gap once it is provably closed", () => {
    // The marker must disappear when the hole it described does.
    const gap = openGap(ns(0), ns(10));
    const gaps = addGap([], gap);
    expect(updateGap(gaps, fillGap(gap, 3, null))).toHaveLength(0);
  });

  it("keeps a partially filled gap in the list", () => {
    const gap = openGap(ns(0), ns(10));
    const gaps = addGap([], gap);
    const next = updateGap(gaps, fillGap(gap, 500, "cursor-1"));
    expect(next).toHaveLength(1);
    expect(next[0].filled).toBe(500);
  });
});

describe("buildRows", () => {
  it("is just the lines when there are no gaps", () => {
    const rows = buildRows([line(1, "a"), line(2, "b")], []);
    expect(rows.map((r) => r.kind)).toEqual(["line", "line"]);
  });

  it("puts the marker between the lines it separates", () => {
    const gap = openGap(ns(1), ns(9));
    const rows = buildRows([line(1, "a"), line(9, "b")], [gap]);
    expect(rows.map((r) => r.kind)).toEqual(["line", "gap", "line"]);
  });

  it("keeps the line AT the lower bound before the marker", () => {
    // afterTs is the newest line we hold BEFORE the gap, so it belongs above
    // the divider, not below it.
    const rows = buildRows(
      [line(1, "a"), line(9, "b")],
      [openGap(ns(1), ns(9))],
    );
    expect(rows[0]).toMatchObject({ kind: "line" });
    expect((rows[0] as { line: LogLine }).line.line).toBe("a");
  });

  it("renders two markers before the same line when both precede it", () => {
    const rows = buildRows(
      [line(1, "a"), line(9, "b")],
      [openGap(ns(1), ns(4)), openGap(ns(4), ns(9))],
    );
    expect(rows.map((r) => r.kind)).toEqual(["line", "gap", "gap", "line"]);
  });

  it("puts a gap newer than every held line at the end", () => {
    // It describes the hole between what we have and the live tail.
    const rows = buildRows([line(1, "a")], [openGap(ns(5), ns(9))]);
    expect(rows.map((r) => r.kind)).toEqual(["line", "gap"]);
  });

  it("orders markers by their lower bound regardless of insertion order", () => {
    const rows = buildRows(
      [line(9, "z")],
      [openGap(ns(5), ns(6)), openGap(ns(1), ns(2))],
    );
    expect(rows.map((r) => r.key)).toEqual([
      openGap(ns(1), ns(2)).id,
      openGap(ns(5), ns(6)).id,
      `${ns(9)}:z`,
    ]);
  });

  it("renders a gap even with no lines at all", () => {
    expect(
      buildRows(EMPTY_RING, [openGap(ns(1), ns(2))]).map((r) => r.kind),
    ).toEqual(["gap"]);
  });
});
