import type { Metadata } from "next";

import { AgentGrid } from "@/components/agents/agent-grid";

export const metadata: Metadata = { title: "Agents" };

// The org chart in motion (Vol 4 §6.5): one card per runtime agent, its state
// and its current work — joined to the declarative roster for tier and
// authority. Clicking a card intercepts into the @drawer slide-over.
export default function Page() {
  return <AgentGrid />;
}
