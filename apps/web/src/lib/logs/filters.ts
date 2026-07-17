/**
 * The filter bar's model, and the client-side half of filtering (Vol 4 §6.7).
 *
 * Two filterings exist and they are NOT the same, which is the thing to keep
 * straight here:
 *
 * - The **backend** filter (`GET /api/v1/logs`) runs in Loki over all of
 *   history. It is the real one.
 * - The **client** filter runs over the ring buffer only — ADR-FRT-05's stated
 *   trade-off: "client memory bounded but filters only search the buffer". It
 *   exists so typing a regex re-filters instantly instead of waiting on a round
 *   trip.
 *
 * They must agree, or a filter would show different lines depending on whether
 * a line happened to be in the buffer. So `matches` below mirrors the LogQL the
 * backend builds — same fields, same case rules — and the tests pin the pairs
 * that are easy to get wrong (level is lowercase; `search` is case-SENSITIVE
 * because Loki's `|=` is; `regex` is unanchored because Loki's `|~` is).
 */
import type { LogLine } from "@titan/api-client";

/**
 * The levels structlog can actually write — the *method* name, lowercased.
 * `warn` becomes `warning` and `exception` becomes `error` inside structlog, so
 * neither ever reaches the wire. §6.7's wireframe shows `INFO`/`WARN`/`ERROR`;
 * none of those three strings exists in the data.
 */
export const LEVELS = [
  "debug",
  "info",
  "warning",
  "error",
  "critical",
] as const;
export type Level = (typeof LEVELS)[number];

/** Severity order, for the "info+" style threshold §6.7's picker implies. */
const SEVERITY: Record<Level, number> = {
  debug: 0,
  info: 1,
  warning: 2,
  error: 3,
  critical: 4,
};

export function isLevel(value: string): value is Level {
  return (LEVELS as readonly string[]).includes(value);
}

/** The levels at or above `floor` — what "Level: info+" means. */
export function levelsAtOrAbove(floor: Level): Level[] {
  return LEVELS.filter((l) => SEVERITY[l] >= SEVERITY[floor]);
}

/** ERROR and CRITICAL are what `e` jumps between and what the drawer opens on. */
export function isErrorLevel(level: string | null | undefined): boolean {
  return level === "error" || level === "critical";
}

export interface LogFilters {
  readonly services: readonly string[];
  readonly levels: readonly Level[];
  readonly projectId: string | null;
  readonly taskId: string | null;
  readonly agentId: string | null;
  readonly search: string | null;
  readonly regex: string | null;
}

export const EMPTY_FILTERS: LogFilters = {
  services: [],
  levels: [],
  projectId: null,
  taskId: null,
  agentId: null,
  search: null,
  regex: null,
};

export function isFiltered(filters: LogFilters): boolean {
  return (
    filters.services.length > 0 ||
    filters.levels.length > 0 ||
    filters.projectId !== null ||
    filters.taskId !== null ||
    filters.agentId !== null ||
    filters.search !== null ||
    filters.regex !== null
  );
}

/**
 * Compile a user's regex, or report why it will not compile.
 *
 * Returns a discriminated result rather than throwing. A half-typed `foo(` is
 * a normal state of the input, not an exceptional one: the box commits on a
 * pause rather than per keystroke, but "paused mid-pattern" is exactly what a
 * debounce catches. Loki uses RE2 and JS uses its own engine, so a pattern can
 * compile here and still be rejected by the backend (lookahead is the usual
 * culprit) — that comes back as a 422 naming the construct, shown inline above
 * the list with the filter bar still mounted.
 */
export type RegexResult =
  | { readonly ok: true; readonly regex: RegExp | null }
  | { readonly ok: false; readonly error: string };

/**
 * Matches the backend's own `_MAX_REGEX`, so a pattern that filters the buffer
 * is also one the history endpoint will accept. A looser cap here would let a
 * 600-character regex visibly filter on screen while every history request
 * 422'd behind it.
 *
 * **It is a compatibility cap, not a ReDoS defence** — worth being precise
 * about, because it looks like one. JavaScript's regex engine backtracks and
 * cannot be interrupted, so a SHORT catastrophic pattern (`(a+)+$` is six
 * characters) against a long line can hang the tab, and no length limit
 * touches that. The real fix is a timeout, which means matching off the main
 * thread in a worker — a bigger change than this screen, and one to make
 * deliberately rather than smuggle in here. Until then the exposure is bounded
 * and stated: the pattern comes from this user's own filter box or a link they
 * chose to open, it reads nothing and sends nothing, and the cost is their own
 * tab. Recorded as a known limitation rather than papered over.
 */
const MAX_PATTERN = 512;

export function compileRegex(pattern: string | null): RegexResult {
  if (!pattern) return { ok: true, regex: null };
  if (pattern.length > MAX_PATTERN) {
    return {
      ok: false,
      error: `Regex is too long (${pattern.length} > ${MAX_PATTERN} characters)`,
    };
  }
  try {
    // Unanchored, like Loki's `|~`. No `g` flag: a lastIndex that persists
    // across `.test()` calls would make matching depend on call order.
    return { ok: true, regex: new RegExp(pattern) };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : "Invalid regex",
    };
  }
}

/**
 * Does this line pass the filter? Mirrors the backend's LogQL.
 *
 * `regex` is passed pre-compiled because this is called once per line per
 * render over up to 50k lines — recompiling inside the loop would dominate.
 */
export function matches(
  line: LogLine,
  filters: LogFilters,
  regex: RegExp | null,
): boolean {
  if (filters.services.length > 0) {
    const service = line.labels?.service;
    if (!service || !filters.services.includes(service)) return false;
  }
  if (filters.levels.length > 0) {
    if (!line.level || !filters.levels.includes(line.level as Level))
      return false;
  }
  // Body-field filters. A line that never bound the field does not match —
  // which is exactly what LogQL's `| json | project_id="x"` does, so the two
  // halves agree. It is also why the UI warns that most lines lack these.
  if (filters.projectId && line.project_id !== filters.projectId) return false;
  if (filters.taskId && line.task_id !== filters.taskId) return false;
  if (filters.agentId && line.agent_id !== filters.agentId) return false;
  // Case-SENSITIVE substring, because Loki's `|=` is. Lower-casing here would
  // quietly return more lines than the same filter run against history.
  if (filters.search && !line.line.includes(filters.search)) return false;
  if (regex && !regex.test(line.line)) return false;
  return true;
}

export function filterLines(
  lines: readonly LogLine[],
  filters: LogFilters,
  regex: RegExp | null,
): readonly LogLine[] {
  // Return the input array itself when nothing is filtered — a new array here
  // would invalidate every downstream memo (and the virtualizer's measurements)
  // on every render for no reason.
  if (!isFiltered(filters)) return lines;
  return lines.filter((line) => matches(line, filters, regex));
}

/** A [start, end) slice of a line that the regex or search matched. */
export interface Highlight {
  readonly start: number;
  readonly end: number;
}

/**
 * Where to draw match highlights in a line (§6.7: "regex highlights matches").
 *
 * Bounded at `limit` ranges: a pattern like `.?` matches at every offset, and a
 * 10k-character line would otherwise mint thousands of spans for one row and
 * stall the frame. Truncating the highlights degrades the decoration; not
 * truncating degrades the whole viewer.
 */
export function highlightRanges(
  text: string,
  filters: LogFilters,
  regex: RegExp | null,
  limit = 100,
): Highlight[] {
  const ranges: Highlight[] = [];

  if (filters.search) {
    let from = 0;
    for (;;) {
      const at = text.indexOf(filters.search, from);
      if (at === -1 || ranges.length >= limit) break;
      ranges.push({ start: at, end: at + filters.search.length });
      from = at + filters.search.length;
    }
  }

  if (regex) {
    // A fresh global regex: the caller's is deliberately non-global (see
    // compileRegex), and mutating theirs would leak lastIndex into `matches`.
    const scanner = new RegExp(
      regex.source,
      regex.flags.includes("g") ? regex.flags : regex.flags + "g",
    );
    let match: RegExpExecArray | null;
    while ((match = scanner.exec(text)) !== null && ranges.length < limit) {
      // A zero-width match (`a*`, `(?:)`) never advances lastIndex, so exec
      // would return the same empty match forever. Step over it by hand.
      if (match[0].length === 0) {
        scanner.lastIndex += 1;
        continue;
      }
      ranges.push({ start: match.index, end: match.index + match[0].length });
    }
  }

  return mergeRanges(ranges);
}

/** Merge overlapping ranges so search and regex hits do not double-wrap. */
function mergeRanges(ranges: Highlight[]): Highlight[] {
  if (ranges.length <= 1) return ranges;
  const sorted = [...ranges].sort((a, b) => a.start - b.start || a.end - b.end);
  const merged: Highlight[] = [sorted[0]];
  for (const range of sorted.slice(1)) {
    const last = merged[merged.length - 1];
    if (range.start <= last.end) {
      merged[merged.length - 1] = {
        start: last.start,
        end: Math.max(last.end, range.end),
      };
    } else {
      merged.push(range);
    }
  }
  return merged;
}

/** Split a line into alternating plain/highlighted segments for rendering. */
export interface Segment {
  readonly text: string;
  readonly match: boolean;
}

export function segment(text: string, ranges: readonly Highlight[]): Segment[] {
  if (ranges.length === 0) return [{ text, match: false }];
  const out: Segment[] = [];
  let at = 0;
  for (const range of ranges) {
    if (range.start > at)
      out.push({ text: text.slice(at, range.start), match: false });
    out.push({ text: text.slice(range.start, range.end), match: true });
    at = range.end;
  }
  if (at < text.length) out.push({ text: text.slice(at), match: false });
  return out;
}
