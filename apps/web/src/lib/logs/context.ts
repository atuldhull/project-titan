/**
 * The ±50-line window behind §6.7's ERROR context drawer.
 *
 * Pure, so the one decision that matters here — what "±50" means at the edges
 * of the buffer — is testable without a DOM.
 */
import type { LogLine } from "@titan/api-client";

import type { Ring } from "@/lib/logs/ring";

/** §6.7: "click ERROR line → context drawer (±50 lines …)". */
export const CONTEXT_RADIUS = 50;

/**
 * The lines around `id`, inclusive, clamped to the buffer.
 *
 * Deliberately does NOT slide the window to keep 101 lines when the target sits
 * near an edge. A line 3 from the start of the buffer has three lines before
 * it; showing 97 after it and calling that "±50" would misrepresent where the
 * line sits in the log. The window is the neighbours that exist.
 */
export function contextWindow(
  ring: Ring,
  id: string,
  radius: number = CONTEXT_RADIUS,
): readonly LogLine[] {
  const at = ring.findIndex((line) => line.id === id);
  if (at === -1) return [];
  return ring.slice(
    Math.max(0, at - radius),
    Math.min(ring.length, at + radius + 1),
  );
}

/** Where the target sits inside the window `contextWindow` returned. */
export function contextOffset(
  ring: Ring,
  id: string,
  radius: number = CONTEXT_RADIUS,
): number {
  const at = ring.findIndex((line) => line.id === id);
  if (at === -1) return -1;
  return at - Math.max(0, at - radius);
}
