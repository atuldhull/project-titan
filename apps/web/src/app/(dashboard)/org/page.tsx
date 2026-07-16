import type { Metadata } from "next";

import { AttentionQueue } from "@/components/notifications/attention-queue";
import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Organization Overview" };

/**
 * Organization Overview (Vol 4 §6.1). The full KPI/activity build-out lands with
 * the Overview screen phase; Phase 57 mounts the Attention Queue here — the §7.1
 * channel for items requiring human action — on top of the new data layer.
 */
export default function Page() {
  return (
    <div className="space-y-6">
      <section className="space-y-3">
        <div>
          <h1 className="text-xl font-semibold">Organization Overview</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Company-wide health: active projects, agent activity, spend, and
            attention items (Vol 4 §6.1).
          </p>
        </div>
        <h2 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
          Needs attention
        </h2>
        <AttentionQueue />
      </section>

      <StubPage
        title="Overview KPIs"
        description="Project health, agent activity and spend tiles arrive with the Overview screen build-out."
      />
    </div>
  );
}
