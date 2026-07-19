import type { Metadata } from "next";

import { MembersScreen } from "@/components/settings/members-screen";

export const metadata: Metadata = { title: "Members" };

/**
 * Settings → Members (Vol 4 §6.13).
 *
 * No RSC prefetch: `member:read` is carved out of the `*:read` wildcard every
 * role holds, so a server prefetch would 403 for most callers and fail the
 * hydration instead of letting the screen render the real permission state.
 */
export default function Page() {
  return <MembersScreen />;
}
