/**
 * Coverage gaps: the "· N lines skipped [load] ·" divider (§6.7).
 *
 * **Why a gap happens here, and why it is not "reconnected".** §6.7 assumes the
 * viewer tails over the WS and that a socket drop is what loses lines. There is
 * no `log.appended` frame in this backend to drop (Vol 4 §5.2's topic table is
 * aspirational — `log.appended`, `agent.heartbeat` and `message.chunk` are all
 * absent), and streaming raw logs over the app socket would be actively harmful:
 * it has a 512-frame send queue, no server-side batching and no rate limit, and
 * an overflowing connection is marked dead and SHED. At §6.7's own target of
 * 1,000 lines/s that socket dies in half a second.
 *
 * So the tail polls the history endpoint forward from its cursor instead. A
 * poll that falls behind simply catches up — it never skips — which means the
 * only way to lose coverage is to *choose* to: when the VIEWER has been away
 * long enough that replaying the backlog would cost many round trips, it JUMPS
 * to now and records the window it stepped over. That window is exact (we know
 * both ends), so [load] fetches precisely it, and the ring's id-dedupe makes
 * the splice duplicate-free by construction.
 *
 * "The viewer has been away" is measured from the last successful POLL, never
 * from the age of the newest line — a quiet system has an old newest-line and a
 * perfectly current tail, and confusing the two marks every idle project as a
 * gap. See `shouldJump`.
 *
 * **On "N lines skipped".** We cannot know N without asking Loki to count, and
 * a count is a second (metric) query over the same range — so the marker names
 * the exact time window it skipped, which we DO know, rather than a number we
 * would have to invent. Once a fill has run, the count is real and is shown.
 */
import type { LogLine } from "@titan/api-client";

import { compareTs, type Ring } from "@/lib/logs/ring";

/**
 * How long the viewer may be away before the tail jumps rather than catches up.
 * A brief stall is normal and catches up in a poll or two; this is about a
 * backgrounded tab or a closed lid.
 */
export const MAX_CATCHUP_MS = 60_000;

/** `BigInt(...)` rather than a `1_000_000n` literal: apps/web targets ES2017,
 *  where the literal syntax is a compile error (the types are fine — `lib`
 *  includes esnext — so only the sugar is unavailable). */
const NS_PER_MS = BigInt(1_000_000);

export interface Gap {
  readonly id: string;
  /** Exclusive lower bound: the newest line we have BEFORE the gap. */
  readonly afterTs: string;
  /** Exclusive upper bound: the oldest line we have AFTER the gap. */
  readonly beforeTs: string;
  readonly status: "idle" | "loading" | "error";
  /** Lines recovered so far. Only meaningful once a fill has run. */
  readonly filled: number;
  /** True once a fill returned no more lines: the gap is provably closed. */
  readonly complete: boolean;
  /**
   * Where the next [load] resumes from, once one page was not enough.
   *
   * Without this a gap wider than a page could never be filled: every click
   * would re-request the SAME first page, re-add the same lines (the ring
   * de-duplicates them away), and inflate `filled` by a page each time — a
   * counter climbing while nothing new arrives.
   */
  readonly cursor: string | null;
  readonly error?: string;
}

export function openGap(afterTs: string, beforeTs: string): Gap {
  return {
    // The bounds identify the gap: re-jumping over the same window must not
    // mint a second marker for it.
    id: `gap:${afterTs}:${beforeTs}`,
    afterTs,
    beforeTs,
    status: "idle",
    filled: 0,
    complete: false,
    cursor: null,
  };
}

/** Nanoseconds between the bounds — what the marker actually displays. */
export function gapDurationMs(gap: Gap): number {
  return Number((BigInt(gap.beforeTs) - BigInt(gap.afterTs)) / NS_PER_MS);
}

/**
 * Should the tail jump over the backlog rather than page through it?
 *
 * Takes **when the tail last succeeded**, not the age of the newest line we
 * hold. Those look interchangeable and are not: a quiet system has an old
 * newest-line while the tail is perfectly current, so measuring the LINE marks
 * every idle project as a gap and litters the viewer with markers over windows
 * that contain nothing. What justifies a jump is the viewer being AWAY —
 * a backgrounded tab, a closed lid — because only then is there a backlog worth
 * skipping.
 *
 * `null` (the tail has not run yet) means catch up normally.
 */
export function shouldJump(
  lastPollAtMs: number | null,
  nowMs: number,
  maxCatchupMs: number = MAX_CATCHUP_MS,
): boolean {
  if (lastPollAtMs === null) return false;
  return nowMs - lastPollAtMs > maxCatchupMs;
}

/**
 * Record a fill's outcome.
 *
 * `cursor` is the backend's `next_cursor`: present means the window held more
 * than one page and the next [load] must resume from it. Absent proves the
 * window is exhausted, which is the only thing that closes the gap.
 */
export function fillGap(
  gap: Gap,
  recovered: number,
  cursor: string | null,
): Gap {
  return {
    ...gap,
    status: "idle",
    filled: gap.filled + recovered,
    complete: cursor === null,
    cursor,
    error: undefined,
  };
}

export function failGap(gap: Gap, error: string): Gap {
  return { ...gap, status: "error", error };
}

export function loadingGap(gap: Gap): Gap {
  return { ...gap, status: "loading", error: undefined };
}

/** Replace a gap in the list by id, dropping it once provably closed. */
export function updateGap(gaps: readonly Gap[], next: Gap): readonly Gap[] {
  if (next.complete) return gaps.filter((g) => g.id !== next.id);
  return gaps.map((g) => (g.id === next.id ? next : g));
}

export function addGap(gaps: readonly Gap[], gap: Gap): readonly Gap[] {
  return gaps.some((g) => g.id === gap.id) ? gaps : [...gaps, gap];
}

/**
 * A row in the virtualized list: a log line, or a gap divider between two.
 *
 * The list renders rows, not lines, so a marker occupies a real row with a real
 * height — the virtualizer measures it like any other. Overlaying the divider
 * instead would misreport the list's total size and drift the scroll position.
 */
export type LogRow =
  | { readonly kind: "line"; readonly key: string; readonly line: LogLine }
  | { readonly kind: "gap"; readonly key: string; readonly gap: Gap };

/**
 * Interleave gap markers into the line list at their boundaries.
 *
 * A gap sits immediately before the first line newer than `afterTs`. A gap
 * whose lines have all been recovered has no boundary left to sit at and is
 * simply not rendered — the marker disappears when the hole it described does.
 */
export function buildRows(lines: Ring, gaps: readonly Gap[]): LogRow[] {
  if (gaps.length === 0) {
    return lines.map((line) => ({ kind: "line", key: line.id, line }));
  }

  const pending = [...gaps].sort((a, b) => compareTs(a.afterTs, b.afterTs));
  const rows: LogRow[] = [];
  let next = 0;

  for (const line of lines) {
    // A line can be preceded by more than one marker if two short gaps were
    // recorded before either was filled.
    while (
      next < pending.length &&
      compareTs(line.ts_ns, pending[next].afterTs) > 0
    ) {
      rows.push({ kind: "gap", key: pending[next].id, gap: pending[next] });
      next += 1;
    }
    rows.push({ kind: "line", key: line.id, line });
  }

  // A gap newer than every line we hold still belongs at the end: it describes
  // the hole between what we have and the live tail.
  for (; next < pending.length; next += 1) {
    rows.push({ kind: "gap", key: pending[next].id, gap: pending[next] });
  }
  return rows;
}
