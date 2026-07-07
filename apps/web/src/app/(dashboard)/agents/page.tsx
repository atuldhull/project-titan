import type { Metadata } from "next";

import { AgentRoster } from "@/components/agents/agent-roster";

export const metadata: Metadata = { title: "Agents" };

// The 16-agent roster grid (Vol 4 §6.5), wired live to the Phase-26 endpoint.
// Runtime status (current task, live KPIs) and the intercepted detail drawer
// arrive with the full dashboard build-out (M6).
export default function Page() {
  return <AgentRoster />;
}
