"use client";

import { useEffect, useState } from "react";

/**
 * A coarse clock, for readouts that must advance on their own.
 *
 * The §6.5 card shows "▓ 2m 10s" — elapsed on the current run. That is a
 * function of `Date.now()`, and nothing else on the screen re-renders between
 * `run.started` and `run.finished`: the agent list is invalidated only by
 * pause/resume, definitions never go stale, and React Query's structural
 * sharing means even a refetch returning identical bytes hands back the SAME
 * object, so a `useMemo` over query data does not recompute. Sampling
 * `Date.now()` inside that memo therefore freezes it at whatever it read when
 * the run started — about 0s — for the run's entire life.
 *
 * So the clock has to be its own state. 10s, not 1s: the label is only
 * meaningful to the nearest several seconds, and a per-second tick would
 * re-render every card in the grid sixty times a minute to move one word.
 */
export const TICK_MS = 10_000;

export function useNowMs(intervalMs: number = TICK_MS): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const handle = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(handle);
  }, [intervalMs]);
  return now;
}
