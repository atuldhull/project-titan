import { AgentDrawer } from "@/components/agents/agent-drawer";

/**
 * The intercepted drawer over /agents.
 *
 * Only reached by a client-side navigation from the grid. A hard load of the
 * same URL renders `agents/[agentId]/page.tsx` full-page instead.
 */
export default async function Page({
  params,
}: {
  params: Promise<{ agentId: string }>;
}) {
  const { agentId } = await params;
  return <AgentDrawer agentId={agentId} />;
}
