/**
 * The filter bar's logic, and its agreement with the backend's LogQL.
 *
 * The client filter searches the ring buffer; the backend filter searches all
 * of Loki. They must return the same verdict for the same line, or a filter
 * would show different results depending on whether a line happened to be in
 * the buffer. The cases below are the ones where that agreement is easy to
 * break.
 */
import { describe, expect, it } from "vitest";

import type { LogLine } from "@titan/api-client";

import {
  compileRegex,
  EMPTY_FILTERS,
  filterLines,
  highlightRanges,
  isErrorLevel,
  isFiltered,
  isLevel,
  LEVELS,
  levelsAtOrAbove,
  matches,
  segment,
  type LogFilters,
} from "@/lib/logs/filters";

function line(over: Partial<LogLine> = {}): LogLine {
  return {
    id: "1:a",
    ts_ns: "1700000000000000000",
    line: "starting payment module",
    labels: { namespace: "titan", service: "api", level: "info" },
    level: "info",
    event: "starting payment module",
    module: "titan.runs.service",
    request_id: null,
    trace_id: null,
    span_id: null,
    project_id: null,
    task_id: null,
    agent_id: null,
    fields: {},
    ...over,
  } as LogLine;
}

const filters = (over: Partial<LogFilters> = {}): LogFilters => ({
  ...EMPTY_FILTERS,
  ...over,
});

describe("levels: the vocabulary that actually exists", () => {
  it("is structlog's lowercase method names", () => {
    expect(LEVELS).toEqual(["debug", "info", "warning", "error", "critical"]);
  });

  it("rejects the spellings §6.7's wireframe shows", () => {
    // The wireframe reads INFO/WARN/ERROR. None of those three strings exists
    // in the data: structlog writes the method name, and `warn` -> `warning`.
    expect(isLevel("WARN")).toBe(false);
    expect(isLevel("warn")).toBe(false);
    expect(isLevel("INFO")).toBe(false);
    expect(isLevel("warning")).toBe(true);
  });

  it("expands a threshold into the levels at or above it", () => {
    expect(levelsAtOrAbove("warning")).toEqual([
      "warning",
      "error",
      "critical",
    ]);
    expect(levelsAtOrAbove("debug")).toEqual([...LEVELS]);
  });

  it("treats critical as an error for jump-to-next-error", () => {
    expect(isErrorLevel("error")).toBe(true);
    expect(isErrorLevel("critical")).toBe(true);
    expect(isErrorLevel("warning")).toBe(false);
    expect(isErrorLevel(null)).toBe(false);
  });
});

describe("matches", () => {
  it("passes everything when nothing is filtered", () => {
    expect(matches(line(), EMPTY_FILTERS, null)).toBe(true);
    expect(isFiltered(EMPTY_FILTERS)).toBe(false);
  });

  it("filters on the service LABEL, not the body's service field", () => {
    // The Loki label is the COMPOSE service (`api`); the JSON body's `service`
    // is `titan-api`. Different vocabularies for the same process.
    expect(matches(line(), filters({ services: ["api"] }), null)).toBe(true);
    expect(matches(line(), filters({ services: ["worker"] }), null)).toBe(
      false,
    );
  });

  it("does not match a line whose stream has no level label", () => {
    // postgres and redis log plain text, so promtail's json stage extracts no
    // level and those streams carry no level label at all.
    expect(
      matches(line({ level: null }), filters({ levels: ["info"] }), null),
    ).toBe(false);
  });

  it("is case-SENSITIVE on search, because Loki's |= is", () => {
    // Lower-casing here would quietly return more lines than the same filter
    // run against history.
    expect(matches(line(), filters({ search: "payment" }), null)).toBe(true);
    expect(matches(line(), filters({ search: "PAYMENT" }), null)).toBe(false);
  });

  it("excludes a line that never bound the body field", () => {
    // Exactly what LogQL's `| json | project_id="x"` does: an absent field does
    // not match. Most lines have no project_id, so this filter looks broken
    // while being correct — hence the warning in the UI.
    expect(
      matches(line({ project_id: null }), filters({ projectId: "p1" }), null),
    ).toBe(false);
    expect(
      matches(line({ project_id: "p1" }), filters({ projectId: "p1" }), null),
    ).toBe(true);
  });

  it("matches a regex unanchored, like Loki's |~", () => {
    const compiled = compileRegex("payment");
    expect(
      compiled.ok &&
        matches(line(), filters({ regex: "payment" }), compiled.regex),
    ).toBe(true);
  });

  it("ands every active filter together", () => {
    const f = filters({
      services: ["api"],
      levels: ["info"],
      search: "payment",
    });
    expect(matches(line(), f, null)).toBe(true);
    expect(matches(line({ level: "error" }), f, null)).toBe(false);
  });
});

describe("compileRegex", () => {
  it("reports a broken pattern instead of throwing", () => {
    // This runs on every keystroke; `foo(` is the normal state of a half-typed
    // input, not an exceptional condition.
    const result = compileRegex("foo(");
    expect(result.ok).toBe(false);
  });

  it("has no global flag, so test() does not depend on call order", () => {
    const result = compileRegex("a");
    expect(result.ok && result.regex?.flags).not.toContain("g");
    // A `g` regex carries lastIndex between calls: the 2nd test() would start
    // mid-string and miss.
    const r = compileRegex("a");
    if (r.ok && r.regex) {
      expect(r.regex.test("aa")).toBe(true);
      expect(r.regex.test("aa")).toBe(true);
    }
  });

  it("treats an empty pattern as no filter", () => {
    const result = compileRegex(null);
    expect(result.ok && result.regex).toBeNull();
  });
});

describe("filterLines", () => {
  it("returns the input array itself when nothing is filtered", () => {
    // A fresh array would invalidate every downstream memo — and the
    // virtualizer's measurements — on every render, for nothing.
    const lines = [line()];
    expect(filterLines(lines, EMPTY_FILTERS, null)).toBe(lines);
  });

  it("filters when a filter is active", () => {
    const lines = [
      line({ id: "a", level: "info" }),
      line({ id: "b", level: "error" }),
    ];
    expect(
      filterLines(lines, filters({ levels: ["error"] }), null),
    ).toHaveLength(1);
  });
});

describe("highlightRanges", () => {
  it("finds every occurrence of a search term", () => {
    expect(highlightRanges("ab ab", filters({ search: "ab" }), null)).toEqual([
      { start: 0, end: 2 },
      { start: 3, end: 5 },
    ]);
  });

  it("does not hang on a zero-width regex match", () => {
    // `a*` matches empty at every offset and never advances lastIndex, so a
    // naive exec loop spins forever.
    const compiled = compileRegex("a*");
    const ranges = compiled.ok
      ? highlightRanges("bbb", filters({ regex: "a*" }), compiled.regex)
      : [];
    expect(ranges).toEqual([]);
  });

  it("caps the number of ranges", () => {
    // A long line matching at hundreds of offsets would otherwise mint
    // hundreds of spans for one row and stall the frame.
    //
    // The matches must be NON-ADJACENT or this proves nothing. The first draft
    // ran /./ over "xxxx…": its 1000 hits all touch, mergeRanges collapses
    // them into ONE range, and `length <= 10` then held with the cap deleted.
    // "x " separates every hit, so only the cap can keep the count down.
    const compiled = compileRegex("x");
    const ranges = compiled.ok
      ? highlightRanges(
          "x ".repeat(500),
          filters({ regex: "x" }),
          compiled.regex,
          10,
        )
      : [];
    expect(ranges.length).toBe(10);
  });

  it("keeps non-adjacent matches separate, so the cap is load-bearing", () => {
    // Guards the test above: proves this fixture really does produce 500
    // distinct ranges, and would blow past the cap without one.
    const compiled = compileRegex("x");
    const ranges = compiled.ok
      ? highlightRanges(
          "x ".repeat(500),
          filters({ regex: "x" }),
          compiled.regex,
          1000,
        )
      : [];
    expect(ranges.length).toBe(500);
  });

  it("merges overlapping search and regex hits", () => {
    const compiled = compileRegex("pay");
    const ranges = compiled.ok
      ? highlightRanges(
          "payment",
          filters({ search: "payme", regex: "pay" }),
          compiled.regex,
        )
      : [];
    expect(ranges).toEqual([{ start: 0, end: 5 }]);
  });

  it("does not mutate the caller's regex", () => {
    // highlightRanges needs a global scanner; mutating the shared instance
    // would leak lastIndex into `matches` and make filtering order-dependent.
    const compiled = compileRegex("a");
    if (compiled.ok && compiled.regex) {
      highlightRanges("aaa", filters({ regex: "a" }), compiled.regex);
      expect(compiled.regex.lastIndex).toBe(0);
      expect(compiled.regex.test("a")).toBe(true);
    }
  });
});

describe("segment", () => {
  it("splits a line into plain and matched runs", () => {
    expect(segment("abcd", [{ start: 1, end: 3 }])).toEqual([
      { text: "a", match: false },
      { text: "bc", match: true },
      { text: "d", match: false },
    ]);
  });

  it("returns one plain run when nothing matched", () => {
    expect(segment("abcd", [])).toEqual([{ text: "abcd", match: false }]);
  });

  it("handles a match at the very start and end", () => {
    expect(segment("ab", [{ start: 0, end: 2 }])).toEqual([
      { text: "ab", match: true },
    ]);
  });
});
