import { describe, expect, it } from "vitest";

import {
  broadcastNotification,
  subscribeNotificationBroadcast,
  type NotifyBroadcast,
} from "@/lib/notifications/broadcast";

/**
 * TITAN-FRT-038: a mark-read in one tab must reach every other tab (which then
 * invalidates its unread query, converging the badge well inside the 2 s bound).
 * Two BroadcastChannel objects stand in for two tabs.
 */
describe("cross-tab notification broadcast", () => {
  it("delivers a mark-read from one tab to another tab's listener", async () => {
    const received: NotifyBroadcast[] = [];
    const unsubscribe = subscribeNotificationBroadcast((m) => received.push(m));

    broadcastNotification({ type: "read" });
    await new Promise((resolve) => setTimeout(resolve, 50));

    unsubscribe();
    expect(received).toEqual([{ type: "read" }]);
  });

  it("delivers mark-all-read and stops after unsubscribe", async () => {
    const received: NotifyBroadcast[] = [];
    const unsubscribe = subscribeNotificationBroadcast((m) => received.push(m));

    broadcastNotification({ type: "read-all" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(received).toEqual([{ type: "read-all" }]);

    unsubscribe();
    broadcastNotification({ type: "read" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(received).toHaveLength(1); // no delivery after close
  });
});
