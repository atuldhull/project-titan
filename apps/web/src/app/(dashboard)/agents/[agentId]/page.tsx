import type { Metadata } from "next";

import { StubPage } from "@/components/shell/stub-page";

export const metadata: Metadata = { title: "Agent" };

// Rendered as an intercepted drawer over /agents in M6 (Vol 4 §11); a plain
// route for now so deep links already resolve (TITAN-FRT-006).
export default async function Page({
  params,
}: {
  params: Promise<{ agentId: string }>;
}) {
  const { agentId } = await params;
  return (
    <StubPage
      title="Agent Detail"
      description="Role, current task, tool grants, KPIs, and recent runs for one agent (Vol 4 §6.5)."
    >
      <p className="mt-3 font-mono text-xs text-muted-foreground">
        agent: {agentId}
      </p>
    </StubPage>
  );
}
