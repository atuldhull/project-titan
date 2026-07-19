import type { Metadata } from "next";

import { NotificationsScreen } from "@/components/settings/notifications-screen";

export const metadata: Metadata = { title: "Notification Settings" };

/** Settings → Notifications (Vol 4 §6.13, the §7.1 category × channel matrix). */
export default function Page() {
  return <NotificationsScreen />;
}
