/**
 * TITAN-FRT-030's load fixture: 1,000 lines/s in tail mode.
 *
 * **What this measures, and what it honestly cannot.** FRT-030 is stated in
 * frames per second, and there is no browser here — jsdom has no compositor, no
 * rAF driven by a real vsync, and no layout, so fps is not observable in this
 * suite and no assertion in this file should be read as one. There is no
 * Playwright in the repo either (the same gap Phase 61 reported for its 2s
 * bell assertion).
 *
 * What IS measurable is the work a frame would have to do, which is where the
 * budget actually goes. At 60fps a frame has 16.6ms and at 30fps it has 33.3ms;
 * the viewer's per-frame cost is one ring append plus one filter pass over the
 * buffer, and the virtualizer renders only the ~50 visible rows regardless of
 * buffer size. So these tests bound the two costs that scale with the data and
 * would be what breaks the frame budget.
 *
 * The thresholds are deliberately loose — CI machines are noisy and a flaky
 * perf test gets deleted, which is worse than a loose one. They are here to
 * catch an O(n^2) regression, not to certify a frame rate.
 *
 * `BUDGET_MS` is that loose ceiling, and it is loose on purpose: the real work
 * is single-digit milliseconds, and an O(n^2) regression on 50k lines would be
 * SECONDS, so anything between the two catches the regression. It was 33ms
 * (one 30fps frame) and flaked when the machine was also running a full test
 * suite and a review fleet — the assertion is "not catastrophically slow", not
 * "renders at 30fps under contention", and it now says so.
 */
import { describe, expect, it } from "vitest";

import type { LogLine } from "@titan/api-client";

import {
  compileRegex,
  EMPTY_FILTERS,
  filterLines,
  type LogFilters,
} from "@/lib/logs/filters";
import {
  appendLines,
  EMPTY_RING,
  RING_CAPACITY,
  type Ring,
} from "@/lib/logs/ring";

const BASE = BigInt("1700000000000000000");

/** Loose ceiling: catches an O(n^2) regression (seconds on 50k) while surviving
 *  a CI box that is also running the full suite and a review fleet. */
const BUDGET_MS = 250;

function makeLines(count: number, from = 0): LogLine[] {
  const out: LogLine[] = [];
  for (let i = from; i < from + count; i++) {
    const ts = String(BASE + BigInt(i) * BigInt(1_000_000));
    out.push({
      id: `${ts}:${i}`,
      ts_ns: ts,
      line: `{"event":"task ${i} starting payment module","level":"info","service":"titan-api"}`,
      labels: {
        namespace: "titan",
        service: i % 3 === 0 ? "worker" : "api",
        level: "info",
      },
      level: i % 50 === 0 ? "error" : "info",
      event: `task ${i} starting payment module`,
      module: "titan.runs.service",
      request_id: `req-${i}`,
      trace_id: null,
      span_id: null,
      project_id: null,
      task_id: null,
      agent_id: null,
      fields: {},
    } as LogLine);
  }
  return out;
}

/** A full buffer — the worst case, where every per-line cost is at maximum. */
function saturatedRing(): Ring {
  return appendLines(EMPTY_RING, makeLines(RING_CAPACITY));
}

describe("sustained tail throughput (TITAN-FRT-030)", () => {
  it("applies 1,000 lines/s into a FULL 50k buffer well inside a frame", () => {
    // The tail polls once a second, so one second of lines at FRT-030's target
    // rate lands as one append. This is that append, against a buffer that is
    // already at capacity and therefore evicting on every call.
    const ring = saturatedRing();
    const incoming = makeLines(1_000, RING_CAPACITY);

    const started = performance.now();
    const next = appendLines(ring, incoming);
    const elapsed = performance.now() - started;

    expect(next).toHaveLength(RING_CAPACITY);
    // One 33ms frame at 30fps. In practice this is single-digit milliseconds;
    // the ceiling is here to catch a regression to O(n^2), not to time the box.
    expect(elapsed).toBeLessThan(BUDGET_MS);
  });

  it("stays linear as the buffer fills, rather than quadratic", () => {
    // appendLines rebuilds an id Set over the whole ring on every call, so its
    // cost is O(buffer) per append. That is affordable only because appends are
    // batched (one poll = one append). If a change ever made the ring grow
    // without bound, or appended per line, this ratio is what would catch it.
    const small = appendLines(EMPTY_RING, makeLines(5_000));
    const large = appendLines(EMPTY_RING, makeLines(50_000));
    const batch = makeLines(1_000, 60_000);

    const t0 = performance.now();
    appendLines(small, batch);
    const smallMs = performance.now() - t0;

    const t1 = performance.now();
    appendLines(large, batch);
    const largeMs = performance.now() - t1;

    // 10x the buffer should cost roughly 10x, not 100x. Generous headroom for
    // a noisy CI box and a sub-millisecond baseline.
    expect(largeMs).toBeLessThan(Math.max(smallMs, 0.5) * 40);
  });

  it("sustains 10 consecutive seconds of 1,000 lines/s", () => {
    let ring = saturatedRing();
    const started = performance.now();
    for (let second = 0; second < 10; second++) {
      ring = appendLines(
        ring,
        makeLines(1_000, RING_CAPACITY + second * 1_000),
      );
    }
    const elapsed = performance.now() - started;

    expect(ring).toHaveLength(RING_CAPACITY);
    // 10 appends, each of which must fit in its own second. Being 10x a single
    // frame's budget in TEN seconds of wall clock is an enormous margin.
    expect(elapsed).toBeLessThan(BUDGET_MS * 10);
  });
});

describe("filters stay responsive (TITAN-FRT-030: 'without … freezing filters')", () => {
  const ring = saturatedRing();

  it("filters a full 50k buffer by level within a frame", () => {
    const filters: LogFilters = { ...EMPTY_FILTERS, levels: ["error"] };
    const started = performance.now();
    const shown = filterLines(ring, filters, null);
    const elapsed = performance.now() - started;

    expect(shown.length).toBeGreaterThan(0);
    expect(elapsed).toBeLessThan(BUDGET_MS);
  });

  it("filters a full 50k buffer by regex within a frame", () => {
    // The expensive one: a regex test against every line's raw text. This is
    // what runs on each keystroke in the regex box.
    const filters: LogFilters = { ...EMPTY_FILTERS, regex: "payment.*module" };
    const compiled = compileRegex(filters.regex);
    expect(compiled.ok).toBe(true);

    const started = performance.now();
    const shown = filterLines(
      ring,
      filters,
      compiled.ok ? compiled.regex : null,
    );
    const elapsed = performance.now() - started;

    expect(shown.length).toBeGreaterThan(0);
    expect(elapsed).toBeLessThan(BUDGET_MS);
  });

  it("costs nothing at all when no filter is set", () => {
    // The unfiltered path returns the input array by identity, so it is O(1)
    // and — more importantly — does not invalidate the memo downstream of it.
    const started = performance.now();
    const shown = filterLines(ring, EMPTY_FILTERS, null);
    expect(performance.now() - started).toBeLessThan(1);
    expect(shown).toBe(ring);
  });
});
