"use client";

import { toast } from "sonner";

import type { NotificationOut } from "@titan/api-client";

import { TOAST_AUTODISMISS_MS, useToastPolicy } from "@/stores/toasts";
import { isCritical, routeNotification } from "./taxonomy";

/**
 * Render a notification as a sonner toast per the §7.1 policy (TITAN-FRT-039/040)
 * — only if its category routes to a toast. `critical` toasts persist (Infinity
 * duration); ≥ 4 same-category within 10 s collapse to one grouped toast. The
 * decision is made by the pure toast policy so the rules are unit-tested.
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
    if (critical) {
      toast.error(
        `${decision.count} ${notification.category} updates`,
        options,
      );
    } else {
      toast(`${decision.count} ${notification.category} updates`, options);
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
}
