"use client";

/**
 * Cross-tab notification sync (TITAN-FRT-038). A mark-read in one tab posts on a
 * BroadcastChannel; every other tab invalidates its unread query and updates the
 * badge in ≤ 2 s. Guarded for environments without BroadcastChannel (SSR/tests).
 */
const CHANNEL = "titan.notifications";

export type NotifyBroadcast = { type: "read" | "read-all" };

export function broadcastNotification(message: NotifyBroadcast): void {
  if (typeof BroadcastChannel === "undefined") {
    return;
  }
  const channel = new BroadcastChannel(CHANNEL);
  channel.postMessage(message);
  channel.close();
}

export function subscribeNotificationBroadcast(
  onMessage: (message: NotifyBroadcast) => void,
): () => void {
  if (typeof BroadcastChannel === "undefined") {
    return () => {};
  }
  const channel = new BroadcastChannel(CHANNEL);
  channel.onmessage = (event: MessageEvent<NotifyBroadcast>) =>
    onMessage(event.data);
  return () => channel.close();
}
