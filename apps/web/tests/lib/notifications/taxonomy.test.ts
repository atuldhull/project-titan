import { describe, expect, it } from "vitest";

import {
  CATEGORY_DEFAULTS,
  routeNotification,
} from "@/lib/notifications/taxonomy";

describe("notification taxonomy (Vol 4 §7.1)", () => {
  it("routes approval + security to toast + bell + attention queue", () => {
    for (const category of ["approval", "security"] as const) {
      expect(routeNotification(category)).toEqual({
        toast: true,
        bell: true,
        attentionQueue: true,
        feedOnly: false,
      });
      expect(CATEGORY_DEFAULTS[category].severity).toBe("critical");
    }
  });

  it("routes failure to toast + bell only", () => {
    expect(routeNotification("failure")).toEqual({
      toast: true,
      bell: true,
      attentionQueue: false,
      feedOnly: false,
    });
  });

  it("routes progress to bell only, activity to feed only", () => {
    expect(routeNotification("progress")).toEqual({
      toast: false,
      bell: true,
      attentionQueue: false,
      feedOnly: false,
    });
    expect(routeNotification("activity")).toEqual({
      toast: false,
      bell: false,
      attentionQueue: false,
      feedOnly: true,
    });
  });

  it("falls back to bell-only for an unknown category with no severity", () => {
    expect(routeNotification("mystery")).toEqual({
      toast: false,
      bell: true,
      attentionQueue: false,
      feedOnly: false,
    });
  });

  // The backend's real emitters use categories outside the §7.1 vocabulary —
  // handoffs sends `pipeline` and the plugin supervisor sends `plugin`, both
  // critical. Routing those by severity is what keeps a "pipeline needs human
  // input" alert from degrading into a silent bell row.
  it("routes an unknown CRITICAL category like approval (toast + attention)", () => {
    for (const category of ["pipeline", "plugin"]) {
      expect(routeNotification(category, "critical")).toEqual({
        toast: true,
        bell: true,
        attentionQueue: true,
        feedOnly: false,
      });
    }
  });

  it("routes an unknown HIGH category like failure (toast + bell)", () => {
    expect(routeNotification("pipeline", "high")).toEqual({
      toast: true,
      bell: true,
      attentionQueue: false,
      feedOnly: false,
    });
  });

  it("keeps the §7.1 routing for a known category regardless of severity", () => {
    expect(routeNotification("activity", "critical").feedOnly).toBe(true);
  });
});
