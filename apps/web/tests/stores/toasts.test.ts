import { describe, expect, it } from "vitest";

import { evaluateToast, TOAST_COALESCE_THRESHOLD } from "@/stores/toasts";

describe("toast policy (Vol 4 §7.1, TITAN-FRT-039/040)", () => {
  it("skips categories that don't route to a toast", () => {
    const { decision } = evaluateToast(
      {},
      { category: "activity", severity: "low", toast: false, nowMs: 0 },
    );
    expect(decision).toEqual({ action: "skip" });
  });

  it("shows a normal toast and marks a critical one to persist", () => {
    const normal = evaluateToast(
      {},
      { category: "failure", severity: "high", toast: true, nowMs: 0 },
    );
    expect(normal.decision).toEqual({ action: "show", persist: false });

    const critical = evaluateToast(
      {},
      { category: "approval", severity: "critical", toast: true, nowMs: 0 },
    );
    expect(critical.decision).toEqual({ action: "show", persist: true });
  });

  it("coalesces ≥ 4 same-category toasts inside the 10 s window", () => {
    let recent: Record<string, number[]> = {};
    let last;
    for (let i = 0; i < TOAST_COALESCE_THRESHOLD; i++) {
      const result = evaluateToast(recent, {
        category: "failure",
        severity: "high",
        toast: true,
        nowMs: i * 1000, // all within 10 s
      });
      recent = result.recent;
      last = result.decision;
    }
    expect(last).toEqual({
      action: "coalesce",
      category: "failure",
      count: TOAST_COALESCE_THRESHOLD,
    });
  });

  it("does not coalesce when events are spread beyond the window", () => {
    let recent: Record<string, number[]> = {};
    let last;
    for (let i = 0; i < TOAST_COALESCE_THRESHOLD; i++) {
      const result = evaluateToast(recent, {
        category: "failure",
        severity: "high",
        toast: true,
        nowMs: i * 20_000, // 20 s apart → window never fills
      });
      recent = result.recent;
      last = result.decision;
    }
    expect(last).toEqual({ action: "show", persist: false });
  });
});
