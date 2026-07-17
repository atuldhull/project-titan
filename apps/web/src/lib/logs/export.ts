/**
 * Copy-line and filtered NDJSON export (§6.7).
 *
 * Pure string building — the download itself is the component's job.
 */
import type { LogLine } from "@titan/api-client";

/**
 * What "copy line" copies: the raw line, byte for byte.
 *
 * Not the parsed object. Someone copying a log line is almost always about to
 * paste it into a grep, a ticket or a search box, and the raw text is the thing
 * that matches what is in Loki and in the container's own stdout.
 */
export function copyText(line: LogLine): string {
  return line.line;
}

/**
 * NDJSON: one JSON object per line, newline-terminated.
 *
 * Exports the STRUCTURED line rather than echoing `line` verbatim, because the
 * point of the export is to pipe it into jq. Callers pass the already-filtered
 * list — the export follows what is on screen, per §6.7's "filtered NDJSON",
 * and that is also the only honest option: the buffer is all the client has.
 */
export function toNdjson(lines: readonly LogLine[]): string {
  if (lines.length === 0) return "";
  return lines.map((line) => JSON.stringify(line)).join("\n") + "\n";
}

/** A filename that sorts and does not collide across exports. */
export function exportFilename(nowMs: number): string {
  // Colons are illegal in Windows filenames and get mangled on download, so
  // this is not an ISO timestamp.
  const stamp = new Date(nowMs)
    .toISOString()
    .replace(/[:.]/g, "-")
    .replace("Z", "");
  return `titan-logs-${stamp}.ndjson`;
}

/**
 * Milliseconds from a Loki nanosecond string, for display.
 *
 * `Number(ts_ns)` on the full nanosecond would round: 2**53 is ~9.007e15 and a
 * nanosecond timestamp is ~1.78e18. Dividing as BigInt first keeps the result
 * inside the safe range with millisecond precision, which is all a clock needs.
 *
 * (`BigInt(1_000_000)` rather than the `1_000_000n` literal: apps/web targets
 * ES2017, where the literal syntax is a compile error. The types are available
 * — `lib` includes esnext — so only the sugar is missing.)
 */
const NS_PER_MS = BigInt(1_000_000);

export function tsToMs(tsNs: string): number {
  return Number(BigInt(tsNs) / NS_PER_MS);
}

/** `12:04:11.201` — the wireframe's time column. */
export function formatClock(tsNs: string): string {
  const date = new Date(tsToMs(tsNs));
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  const ss = String(date.getSeconds()).padStart(2, "0");
  const ms = String(date.getMilliseconds()).padStart(3, "0");
  return `${hh}:${mm}:${ss}.${ms}`;
}
