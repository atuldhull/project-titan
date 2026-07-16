import type { Metadata } from "next";

import { OverviewScreen } from "@/components/overview/overview-screen";

export const metadata: Metadata = { title: "Organization Overview" };

/** Organization Overview (Vol 4 §6.1) — live on the Phase-57 data layer. */
export default function Page() {
  return <OverviewScreen />;
}
