"use client";

import { toast } from "sonner";

import type { NotificationOut } from "@titan/api-client";

import { TOAST_AUTODISMISS_MS, useToastPolicy } from "@/stores/toasts";
import { announceCritical } from "./announce";
import { isCritical, routeNotification } from "./taxonomy";

/**
 * Render a notification as a sonner toast per the §7.1 policy (TITAN-FRT-039/040)
 * — only if its category routes to a toast. `critical` toasts persist (Infinity
 * duration); ≥ 4 same-category within 10 s collapse to one grouped toast. The
 * decision is made by the pure toast policy so the rules are unit-tested.
 *
 * TITAN-FRT-043 politeness: sonner announces its whole stack through one
 * hardcoded `aria-live="polite"` container, which is exactly right for ordinary
 * toasts and wrong for the criticals — and sonner 1.7.4 offers no way to change
 * it (see lib/notifications/announce.ts for the verification). So criticals are
 * ALSO pushed to `announceCritical`, which drives a separate assertive region.
 * Politeness is decided by `isCritical` alone, never by which toast style was
 * used: `toast.error` is called for "high" too, and a high-severity failure is
 * not licence to interrupt whatever a screen-reader user is in the middle of.
 */
export function showNotificationToast(
  notification: NotificationOut,
  nowMs: number = Date.now(),
): void {
  const routing = routeNotification(
    notification.category,
    notification.severity,
  );
  const decision = useToastPolicy.getState().evaluate({
    category: notification.category,
    severity: notification.severity,
    toast: routing.toast,
    id: notification.id,
    nowMs,
  });

  if (decision.action === "skip") {
    return;
  }
  if (decision.action === "coalesce") {
    // A grouped burst of criticals must still not auto-dismiss — collapsing them
    // is a display concern and must not quietly downgrade FRT-039's persistence.
    const critical = isCritical(notification.severity);
    const options = {
      id: `coalesce:${notification.category}`,
      duration: critical ? Infinity : TOAST_AUTODISMISS_MS,
    };
    const summary = `${decision.count} ${notification.category} updates`;
    if (critical) {
      toast.error(summary, options);
      // A coalesced burst of criticals is still critical. Collapsing them is a
      // DISPLAY decision; letting it also downgrade the announcement would mean
      // the louder the alert storm, the quieter the screen reader gets.
      announceCritical(summary);
    } else {
      toast(summary, options);
    }
    return;
  }

  const options = {
    id: notification.id,
    duration: decision.persist ? Infinity : TOAST_AUTODISMISS_MS,
    description: notification.body ?? undefined,
    onDismiss: () => useToastPolicy.getState().dismissCritical(notification.id),
    action: notification.link
      ? {
          label: "View",
          onClick: () => {
            if (notification.link) {
              window.location.assign(notification.link);
            }
          },
        }
      : undefined,
  };

  if (
    notification.severity === "critical" ||
    notification.severity === "high"
  ) {
    toast.error(notification.title, options);
  } else {
    toast(notification.title, options);
  }

  if (isCritical(notification.severity)) {
    // Body included: the assertive region is a one-shot utterance with no
    // visual counterpart to go back and read, so a bare title ("Approval
    // required") would interrupt the user to tell them nothing actionable.
    announceCritical(
      notification.body
        ? `${notification.title}. ${notification.body}`
        : notification.title,
    );
  }
}
