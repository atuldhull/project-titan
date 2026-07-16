"use client";

import { create } from "zustand";

import { isCritical } from "@/lib/notifications/taxonomy";

/**
 * Toast policy state (Vol 4 §7.1, TITAN-FRT-039/040).
 *
 * Rendering is sonner (bottom-right, `visibleToasts=3`, 6 s auto-dismiss); the
 * rules sonner can't express live here as pure, testable logic:
 *   - coalescing: ≥ 4 same-category toasts within 10 s collapse to one grouped
 *     toast (FRT-040);
 *   - `critical` toasts never auto-dismiss (Infinity duration) — they stay until
 *     the user dismisses them.
 *
 * HONEST LIMITATION (FRT-039, partially met): "critical persists" IS enforced,
 * but "never displaced by a lower-severity toast" is NOT. sonner stacks by
 * recency, so three newer low-severity toasts push a critical past
 * `visibleToasts` and it renders hidden (opacity:0, pointer-events:none) until
 * the burst subsides — it is never destroyed, but it can be temporarily
 * invisible AND un-dismissable. Enforcing severity ordering needs a custom
 * renderer (or a sonner priority API) and is deliberately not attempted here.
 * `activeCriticals` below records which criticals are outstanding so that
 * renderer can be built; nothing consumes it yet. The durable mitigation is that
 * every critical is also persisted server-side and mirrored to the bell and the
 * Attention Queue, so it is never LOST — only possibly not on screen.
 */
export const TOAST_COALESCE_WINDOW_MS = 10_000;
export const TOAST_COALESCE_THRESHOLD = 4;
export const TOAST_AUTODISMISS_MS = 6_000;
export const TOAST_MAX_VISIBLE = 3;

export type ToastDecision =
  | { action: "skip" }
  | { action: "show"; persist: boolean }
  | { action: "coalesce"; category: string; count: number };

export interface ToastInput {
  category: string;
  severity: string;
  /** Whether this category routes to a toast at all (from the taxonomy). */
  toast: boolean;
  nowMs: number;
}

/** Pure policy: decide show / coalesce / skip and advance the sliding window. */
export function evaluateToast(
  recent: Record<string, number[]>,
  input: ToastInput,
): { decision: ToastDecision; recent: Record<string, number[]> } {
  if (!input.toast) {
    return { decision: { action: "skip" }, recent };
  }
  const prior = (recent[input.category] ?? []).filter(
    (t) => input.nowMs - t < TOAST_COALESCE_WINDOW_MS,
  );
  const next = [...prior, input.nowMs];
  const updated = { ...recent, [input.category]: next };
  if (next.length >= TOAST_COALESCE_THRESHOLD) {
    return {
      decision: {
        action: "coalesce",
        category: input.category,
        count: next.length,
      },
      recent: updated,
    };
  }
  return {
    decision: { action: "show", persist: isCritical(input.severity) },
    recent: updated,
  };
}

interface ToastPolicyState {
  recent: Record<string, number[]>;
  activeCriticals: string[];
  evaluate: (input: ToastInput & { id: string }) => ToastDecision;
  dismissCritical: (id: string) => void;
  reset: () => void;
}

export const useToastPolicy = create<ToastPolicyState>((set, get) => ({
  recent: {},
  activeCriticals: [],
  evaluate: (input) => {
    const { decision, recent } = evaluateToast(get().recent, input);
    set((s) => {
      const persist = decision.action === "show" && decision.persist;
      const activeCriticals =
        persist && !s.activeCriticals.includes(input.id)
          ? [...s.activeCriticals, input.id]
          : s.activeCriticals;
      return { recent, activeCriticals };
    });
    return decision;
  },
  dismissCritical: (id) =>
    set((s) => ({
      activeCriticals: s.activeCriticals.filter((c) => c !== id),
    })),
  reset: () => set({ recent: {}, activeCriticals: [] }),
}));
