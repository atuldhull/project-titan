/**
 * Export, copy, and the nanosecond -> datetime conversions.
 *
 * The rounding tests are the important ones here. The endpoint speaks
 * microsecond-resolution datetimes and Loki speaks nanoseconds, so which way
 * each bound rounds decides whether a window loses lines.
 */
import { describe, expect, it } from "vitest";

import type { LogLine } from "@titan/api-client";

import {
  copyText,
  exportFilename,
  formatClock,
  toNdjson,
  tsToMs,
} from "@/lib/logs/export";
import { nsToIsoCeil, nsToIsoFloor } from "@/lib/logs/hooks";

function line(over: Partial<LogLine> = {}): LogLine {
  return {
    id: "1:a",
    ts_ns: "1700000000000000000",
    line: '{"event":"hello","level":"info"}',
    labels: { service: "api" },
    level: "info",
    event: "hello",
    module: null,
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

describe("copyText", () => {
  it("copies the raw line, not the parsed object", () => {
    // Whoever copies a log line is about to paste it into a grep or a ticket,
    // and the raw text is what matches Loki and the container's own stdout.
    expect(copyText(line())).toBe('{"event":"hello","level":"info"}');
  });
});

describe("toNdjson", () => {
  it("emits one JSON object per line, newline-terminated", () => {
    const out = toNdjson([line({ id: "a" }), line({ id: "b" })]);
    const rows = out.trimEnd().split("\n");
    expect(rows).toHaveLength(2);
    expect(JSON.parse(rows[0]).id).toBe("a");
    expect(out.endsWith("\n")).toBe(true);
  });

  it("is empty for no lines, not a bare newline", () => {
    expect(toNdjson([])).toBe("");
  });

  it("keeps ts_ns a string so jq does not round it", () => {
    const parsed = JSON.parse(
      toNdjson([line({ ts_ns: "1700000000123456789" })]).trim(),
    );
    expect(parsed.ts_ns).toBe("1700000000123456789");
    expect(typeof parsed.ts_ns).toBe("string");
  });
});

describe("exportFilename", () => {
  it("avoids characters that are illegal in a Windows filename", () => {
    // A colon gets mangled on download; an ISO timestamp is full of them.
    const name = exportFilename(Date.UTC(2026, 6, 16, 12, 4, 11));
    expect(name).not.toContain(":");
    expect(name.endsWith(".ndjson")).toBe(true);
  });
});

describe("tsToMs", () => {
  it("does not round the way Number(ts_ns) would", () => {
    // Number("1700000000123456789") is 1700000000123456768 — off by 21ns. The
    // BigInt divide keeps the millisecond exact.
    expect(tsToMs("1700000000123456789")).toBe(1_700_000_000_123);
  });
});

describe("formatClock", () => {
  it("renders the wireframe's HH:MM:SS.mmm", () => {
    expect(
      formatClock(
        String(
          BigInt(Date.UTC(2026, 6, 16, 12, 4, 11, 201)) * BigInt(1_000_000),
        ),
      ),
    ).toMatch(/^\d{2}:\d{2}:\d{2}\.201$/);
  });
});

describe("ns -> ISO rounding: which way each bound goes", () => {
  it("floors the start, so a line inside the boundary microsecond is not skipped", () => {
    // THE bug this exists to prevent: resuming after a line at ...000_000_500
    // must not start at ...000_001_000, or a line at ...000_000_700 — which is
    // genuinely newer — is silently never fetched.
    const start = nsToIsoFloor("1700000000000000500");
    expect(start).toBe("2023-11-14T22:13:20.000000Z");
  });

  it("ceils the end, so a line below the boundary microsecond is not cut off", () => {
    const end = nsToIsoCeil("1700000000000000500");
    expect(end).toBe("2023-11-14T22:13:20.000001Z");
  });

  it("is exact on a nanosecond that is already a whole microsecond", () => {
    expect(nsToIsoFloor("1700000000000001000")).toBe(
      "2023-11-14T22:13:20.000001Z",
    );
    expect(nsToIsoCeil("1700000000000001000")).toBe(
      "2023-11-14T22:13:20.000001Z",
    );
  });

  it("always widens the window, never narrows it", () => {
    // The property that matters: over-fetching is free (the ring de-duplicates
    // by id); under-fetching drops lines with no trace.
    //
    // Read the ISO back at FULL precision. The obvious `new Date(iso).getTime()`
    // truncates to milliseconds, which throws away the very microsecond this
    // test is about — the first draft of it passed with `start` wrongly
    // ceiling, i.e. it proved nothing at all.
    const isoToNs = (iso: string): bigint => {
      const frac = /\.(\d{6})Z$/.exec(iso)![1];
      const seconds = BigInt(Date.parse(`${iso.slice(0, 19)}Z`));
      return seconds * BigInt(1_000_000) + BigInt(frac) * BigInt(1_000);
    };

    const ts = BigInt("1700000000123456789");
    expect(isoToNs(nsToIsoFloor(String(ts)))).toBeLessThan(ts);
    expect(isoToNs(nsToIsoCeil(String(ts)))).toBeGreaterThan(ts);
  });

  it("keeps microsecond digits that a millisecond ISO string would drop", () => {
    expect(nsToIsoFloor("1700000000123456789")).toBe(
      "2023-11-14T22:13:20.123456Z",
    );
  });
});
