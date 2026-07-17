import type { Metadata } from "next";
import { Suspense } from "react";

import { LogsScreen } from "@/components/logs/logs-screen";

export const metadata: Metadata = { title: "Logs" };

/**
 * The Logs Viewer (Vol 4 §6.7, Phase 62).
 *
 * No RSC prefetch, unlike the other screens. Log history is admin-only
 * (`logs:read` is carved out of the `*:read` wildcard every role holds) and
 * Loki is Compose-profile-gated, so a server prefetch would 403 or 503 for most
 * callers on most stacks — the client fetch turns those into the real,
 * actionable states instead of a failed hydration.
 *
 * `?project=` (TITAN-FRT-006) still works and still means the same thing; it is
 * now read inside the screen, because it is one of seven filters that all live
 * in the URL together and the screen writes them back as they change.
 */
export default function Page() {
  return (
    // useSearchParams opts its subtree into client rendering, and Next fails
    // the build for the whole route without a Suspense boundary above it.
    <Suspense fallback={null}>
      <LogsScreen />
    </Suspense>
  );
}
