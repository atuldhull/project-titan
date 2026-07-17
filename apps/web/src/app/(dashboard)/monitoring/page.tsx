import type { Metadata } from "next";

import { MonitoringScreen } from "@/components/monitoring/monitoring-screen";

export const metadata: Metadata = { title: "Monitoring" };

/**
 * Monitoring (Vol 4 §6.10).
 *
 * No RSC prefetch: the range is client state and the ledger query is keyed on
 * it, so a server prefetch would fetch a window the client immediately
 * re-queries.
 */
export default function Page() {
  return <MonitoringScreen />;
}
