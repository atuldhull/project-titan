import { beforeEach, describe, expect, it, vi } from "vitest";

import type { NotificationOut } from "@titan/api-client";

import { useAnnounceStore } from "@/lib/notifications/announce";
import { showNotificationToast } from "@/lib/notifications/notify";
import { useToastPolicy } from "@/stores/toasts";

/**
 * TITAN-FRT-043: "toasts polite, critical alerts assertive".
 *
 * These tests exist because the obvious implementation does not work. sonner
 * 1.7.4 renders its entire stack inside one hardcoded
 * `<section aria-live="polite">` and exposes NO option to change it — there is
 * no `important` field on `ToastT`/`ExternalToast` and the string "assertive"
 * appears nowhere in the package. So the assertive half of the requirement is
 * carried by our own region, and what has to be asserted is that criticals (and
 * ONLY criticals) reach it.
 *
 * sonner is mocked out entirely: this is about which notifications get
 * announced, not about what the toast looks like.
 */
vi.mock("sonner", () => {
  const toast = Object.assign(vi.fn(), { error: vi.fn() });
  return { toast };
});

function notification(over: Partial<NotificationOut> = {}): NotificationOut {
  return {
    id: "n1",
    category: "approval",
    severity: "critical",
    title: "Release v0.4.1 needs approval",
    body: null,
    link: null,
    read_at: null,
    created_at: "2026-07-19T12:00:00Z",
    ...over,
  } as NotificationOut;
}

describe("critical alerts are announced assertively", () => {
  beforeEach(() => {
    useAnnounceStore.getState().reset();
    // The coalescing window is a sliding buffer on a shared store — without
    // this, the fourth notification in the FILE (not the test) coalesces and a
    // test that passes alone fails in-suite.
    useToastPolicy.setState({ recent: {}, activeCriticals: [] });
  });

  it("pushes a critical notification to the assertive region", () => {
    showNotificationToast(notification());
    expect(useAnnounceStore.getState().latest?.text).toBe(
      "Release v0.4.1 needs approval",
    );
  });

  it("includes the body — the region has no visual counterpart to re-read", () => {
    showNotificationToast(
      notification({ body: "Gate G9 passed; awaiting sign-off." }),
    );
    expect(useAnnounceStore.getState().latest?.text).toBe(
      "Release v0.4.1 needs approval. Gate G9 passed; awaiting sign-off.",
    );
  });

  it("does NOT announce high severity — assertive is for critical only", () => {
    // `toast.error` is used for "high" too, so keying politeness off the toast
    // STYLE rather than the severity would interrupt the user for every failure.
    showNotificationToast(
      notification({ id: "n2", category: "failure", severity: "high" }),
    );
    expect(useAnnounceStore.getState().latest).toBeNull();
  });

  it("does NOT announce a notification that routes to no toast at all", () => {
    showNotificationToast(
      notification({ id: "n3", category: "progress", severity: "info" }),
    );
    expect(useAnnounceStore.getState().latest).toBeNull();
  });

  it("re-announces an identical repeat alert", () => {
    // A live region only speaks when its content MUTATES. Two identical
    // criticals must still produce two distinct announcements, which is what
    // the monotonic `seq` is for — the renderer keys on it.
    showNotificationToast(notification());
    const first = useAnnounceStore.getState().latest;
    showNotificationToast(notification({ id: "n1-repeat" }));
    const second = useAnnounceStore.getState().latest;

    expect(second?.text).toBe(first?.text);
    expect(second?.seq).toBeGreaterThan(first?.seq ?? 0);
  });

  it("still announces when a critical burst is COALESCED into one toast", () => {
    // Collapsing 4+ same-category toasts is a display decision (FRT-040).
    // Letting it also suppress the announcement would mean the louder the alert
    // storm, the quieter the screen reader gets.
    for (let i = 0; i < 4; i += 1) {
      showNotificationToast(notification({ id: `burst-${i}` }));
    }
    expect(useAnnounceStore.getState().latest?.text).toBe("4 approval updates");
  });
});
