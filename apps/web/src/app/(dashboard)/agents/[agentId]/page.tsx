import type { Metadata } from "next";
import Link from "next/link";

import { AgentDetailScreen } from "@/components/agents/agent-detail-screen";

export const metadata: Metadata = { title: "Agent" };

/**
 * One agent, full-page.
 *
 * Reached by a hard navigation only — a shared link, a refresh, a new tab. A
 * click from the grid is intercepted into the `@drawer` slide-over instead.
 * Both render the same body, so the two paths cannot drift.
 */
export default async function Page({
  params,
}: {
  params: Promise<{ agentId: string }>;
}) {
  const { agentId } = await params;
  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <Link
        href="/agents"
        className="text-xs text-muted-foreground underline underline-offset-2"
      >
        ← All agents
      </Link>
      <AgentDetailScreen agentId={agentId} />
    </div>
  );
}
