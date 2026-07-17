/**
 * Filters <-> URL (§6.7: every filter URL-encoded and restorable).
 *
 * A URL is user input: typed, truncated, shared in chat, mangled by unfurlers.
 * Half of these are about a hostile or half-eaten one degrading to "no filter"
 * rather than throwing on mount.
 */
import { describe, expect, it } from "vitest";

import { EMPTY_FILTERS, type LogFilters } from "@/lib/logs/filters";
import { filtersToQuery, parseFilters, writeFilters } from "@/lib/logs/url";

const filters = (over: Partial<LogFilters> = {}): LogFilters => ({
  ...EMPTY_FILTERS,
  ...over,
});

describe("round trip", () => {
  it("restores every filter the bar can set", () => {
    const original = filters({
      services: ["api", "worker"],
      levels: ["error", "warning"],
      projectId: "p1",
      taskId: "t1",
      agentId: "eng_backend",
      search: "timeout",
      regex: "pay.*fail",
    });
    expect(parseFilters(writeFilters(original))).toEqual(original);
  });

  it("round-trips an empty filter set to an empty query", () => {
    expect(filtersToQuery(EMPTY_FILTERS)).toBe("");
    expect(parseFilters(new URLSearchParams(""))).toEqual(EMPTY_FILTERS);
  });

  it("survives values needing escaping", () => {
    const original = filters({ search: "a b&c=d#e", regex: "^\\d+\\s" });
    expect(parseFilters(new URLSearchParams(filtersToQuery(original)))).toEqual(
      original,
    );
  });
});

describe("parseFilters is defensive", () => {
  it("drops an unknown level rather than forwarding it", () => {
    // The backend 422s on an unknown level, so forwarding one would make a
    // shared link fail to load instead of loading with a filter ignored.
    expect(
      parseFilters(new URLSearchParams("level=WARN&level=error")).levels,
    ).toEqual(["error"]);
  });

  it("accepts an upper-case level by normalising it", () => {
    // Someone will hand-type ?level=ERROR. Lower-casing is the difference
    // between it working and it silently matching nothing.
    expect(parseFilters(new URLSearchParams("level=ERROR")).levels).toEqual([
      "error",
    ]);
  });

  it("de-duplicates repeated levels", () => {
    expect(
      parseFilters(new URLSearchParams("level=error&level=error")).levels,
    ).toEqual(["error"]);
  });

  it("treats a blank value as absent", () => {
    expect(
      parseFilters(new URLSearchParams("q=&project=%20")).search,
    ).toBeNull();
    expect(
      parseFilters(new URLSearchParams("q=&project=%20")).projectId,
    ).toBeNull();
  });

  it("keeps significant leading and trailing whitespace", () => {
    // Deliberately NOT trimmed. Space is meaningful in both fields a user can
    // type: `" ERROR "` finds the padded level column where `"ERROR"` also
    // matches `"ERRORS"`, and in a regex ` +$` means something quite different
    // from `+$`. Trimming rewrites the filter behind their back, and the
    // client would then filter the buffer on one string while asking Loki for
    // another.
    expect(parseFilters(new URLSearchParams("q=%20timeout%20")).search).toBe(
      " timeout ",
    );
  });

  it("still treats whitespace-only as no filter", () => {
    expect(parseFilters(new URLSearchParams("q=%20%20")).search).toBeNull();
  });

  it("drops an absurdly long value", () => {
    const long = "x".repeat(2000);
    expect(parseFilters(new URLSearchParams(`q=${long}`)).search).toBeNull();
  });

  it("caps a repeated param rather than building an enormous query", () => {
    const params = new URLSearchParams();
    for (let i = 0; i < 100; i++) params.append("service", `svc-${i}`);
    expect(parseFilters(params).services.length).toBeLessThanOrEqual(16);
  });
});

describe("writeFilters", () => {
  it("keeps params it does not own", () => {
    // Dropping an unrelated param every time someone types in the filter box
    // would be a silent, confusing data loss.
    const existing = new URLSearchParams("theme=dark");
    expect(writeFilters(filters({ search: "x" }), existing).get("theme")).toBe(
      "dark",
    );
  });

  it("removes a filter that is no longer set", () => {
    const existing = new URLSearchParams("q=old&level=error");
    const params = writeFilters(EMPTY_FILTERS, existing);
    expect(params.get("q")).toBeNull();
    expect(params.get("level")).toBeNull();
  });

  it("is stable, so an unchanged filter set does not push history", () => {
    // Calling it twice on ONE input only proves the function is deterministic,
    // which any function is — that version passed with `params.sort()` deleted.
    // The real property is that filters differing only in ORDER produce the
    // same string, which is what stops a re-render from churning the URL.
    const a = filters({
      services: ["worker", "api"],
      levels: ["error", "warning"],
    });
    const b = filters({
      services: ["api", "worker"],
      levels: ["warning", "error"],
    });
    expect(filtersToQuery(a)).toBe(filtersToQuery(b));
  });

  it("uses ?project=, the cross-screen param the /logs stub already honoured", () => {
    // FRT-006's spelling. Renaming it to project_id would break every existing
    // deep link into this screen.
    expect(writeFilters(filters({ projectId: "p1" })).get("project")).toBe(
      "p1",
    );
  });
});
