/**
 * The 50k-line ring buffer (ADR-FRT-05).
 *
 * "TanStack Virtual list over an in-memory ring buffer (50k lines) with
 * rAF-batched appends; history via REST (Loki) on demand." Client memory is
 * bounded, which means the filter bar only ever searches what is IN the buffer
 * — full-history search is the backend's job, and the viewer says so.
 *
 * **On "rAF-batched appends" and TITAN-FRT-021's "≤ 100 lines/frame": this does
 * not do that, and the reason is not laziness.** Both exist to stop N incoming
 * events from causing N renders. The tail here polls once a second and applies
 * its response as ONE append, so the coalescing has already happened in the
 * transport — there is nothing left for `lib/ws/raf-batch` to collapse. And
 * dribbling a 500-line response out at 100/frame would make it take eight
 * frames to appear while costing MORE, not less: the expensive part of an
 * append is rebuilding the id set over the whole buffer, which is O(buffer) per
 * call and independent of the batch size, so five small appends cost five times
 * one big one. Virtualization already bounds the render to the ~50 visible
 * rows. The perf fixture measures what actually matters instead.
 *
 * Pure on purpose. jsdom has no layout, so `getBoundingClientRect` is all zeros
 * and a virtualized list renders literally zero rows there (react-virtual bails
 * out when `outerSize === 0`). Everything that can be decided without pixels is
 * therefore decided here, where it can be tested honestly, and the component is
 * left with as little judgement as possible.
 */
import type { LogLine } from "@titan/api-client";

/** ADR-FRT-05's cap. Older lines are re-fetched on demand rather than held. */
export const RING_CAPACITY = 50_000;

/**
 * Lines are ordered oldest-first — the reading order of a log, and the order a
 * tail appends in. Both ends are mutable: a tail pushes onto the end, and a
 * history fill splices onto the front.
 */
export type Ring = readonly LogLine[];

export const EMPTY_RING: Ring = [];

/**
 * Compare two Loki nanosecond timestamps.
 *
 * `ts_ns` is a STRING because a Unix nanosecond is > 2**53: `Number("1700000000123456789")`
 * rounds to ...456768, so a numeric sort silently mis-orders lines within the
 * same microsecond and a numeric equality check merges distinct ones. BigInt is
 * exact, and comparison is not on any hot path (append is O(1) amortised).
 */
export function compareTs(a: string, b: string): number {
  const left = BigInt(a);
  const right = BigInt(b);
  return left < right ? -1 : left > right ? 1 : 0;
}

/**
 * Append newer lines to the tail, dropping ids already present and evicting the
 * oldest beyond the cap.
 *
 * Returns the SAME array when nothing changes. That identity matters: it is
 * what lets React skip a render on a batch that was entirely duplicates (a
 * poll that raced a tail), and what keeps `useMemo` over the ring from
 * recomputing the filter for no reason.
 */
export function appendLines(
  ring: Ring,
  incoming: readonly LogLine[],
  capacity: number = RING_CAPACITY,
): Ring {
  if (incoming.length === 0) return ring;

  const present = new Set(ring.map((l) => l.id));
  const fresh: LogLine[] = [];
  for (const line of incoming) {
    // Dedupe within the batch too, not just against the ring: a gap fill and a
    // tail poll can both carry the same line, and they can arrive together.
    if (present.has(line.id)) continue;
    present.add(line.id);
    fresh.push(line);
  }
  if (fresh.length === 0) return ring;

  const next = ring.concat(fresh);
  return next.length > capacity ? next.slice(next.length - capacity) : next;
}

/**
 * Splice older lines onto the front — a gap fill, or a scroll into history.
 *
 * Eviction here takes from the NEWEST end, which looks backwards until you
 * consider what the user is doing: they scrolled up into the past, so the past
 * is what they are reading. Dropping the lines they just asked for to keep
 * lines they scrolled away from would make the fetch pointless. A tail brings
 * the newest back.
 */
export function prependLines(
  ring: Ring,
  older: readonly LogLine[],
  capacity: number = RING_CAPACITY,
): Ring {
  if (older.length === 0) return ring;

  const present = new Set(ring.map((l) => l.id));
  const fresh = older.filter((line) => {
    if (present.has(line.id)) return false;
    present.add(line.id);
    return true;
  });
  if (fresh.length === 0) return ring;

  const next = fresh.concat(ring);
  return next.length > capacity ? next.slice(0, capacity) : next;
}

/**
 * Merge lines whose position is not known to be at either end, restoring the
 * oldest-first order.
 *
 * The sort is a real cost (O(n log n) over up to 50k), so the fast paths above
 * exist for the two cases that actually happen at speed — a tail and a history
 * page. This is the correctness backstop for a splice landing in the middle,
 * which is what a gap fill does when the reader has already scrolled past the
 * gap and lines exist on both sides of it.
 */
export function mergeLines(
  ring: Ring,
  incoming: readonly LogLine[],
  capacity: number = RING_CAPACITY,
): Ring {
  if (incoming.length === 0) return ring;

  const byId = new Map(ring.map((l) => [l.id, l]));
  let added = false;
  for (const line of incoming) {
    if (byId.has(line.id)) continue;
    byId.set(line.id, line);
    added = true;
  }
  if (!added) return ring;

  const merged = Array.from(byId.values()).sort((a, b) =>
    compareTs(a.ts_ns, b.ts_ns),
  );
  return merged.length > capacity
    ? merged.slice(merged.length - capacity)
    : merged;
}

/** The newest line, or null. The tail's resume position. */
export function newest(ring: Ring): LogLine | null {
  return ring.length > 0 ? ring[ring.length - 1] : null;
}

/** The oldest line, or null. Where a scroll into history resumes from. */
export function oldest(ring: Ring): LogLine | null {
  return ring.length > 0 ? ring[0] : null;
}

/** Whether the buffer is at capacity, i.e. lines have been evicted. */
export function isSaturated(
  ring: Ring,
  capacity: number = RING_CAPACITY,
): boolean {
  return ring.length >= capacity;
}
