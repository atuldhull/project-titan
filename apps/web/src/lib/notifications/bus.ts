"use client";

import type { NotificationOut } from "@titan/api-client";

import { useAttentionStore } from "./attention-store";
import { showNotificationToast } from "./notify";
import { routeNotification } from "./taxonomy";

/**
 * Fan a live `notification.created` frame into the in-app channels per §7.1: a
 * toast (if the category routes to one) and the Attention Queue (for
 * approval/security). The WS manager separately invalidates the notifications
 * queries, so the bell badge and Center refresh from the server — the source of
 * truth for unread state. No cross-tab broadcast here: every tab has its own
 * socket and receives the frame directly.
 */
export function emitNotification(notification: NotificationOut): void {
  const routing = routeNotification(
    notification.category,
    notification.severity,
  );
  showNotificationToast(notification);
  if (routing.attentionQueue) {
    useAttentionStore.getState().add(notification);
  }
}
